"""Shared, conservative ground navigation; never moves a physics body.

Static collision triangles are rasterised during arena loading, not during
combat. Paths use an arena-wide expansion budget. The grid represents the
terrain layer: elevated decks are conservatively obstacles, not invented
connections to the ground below them.
"""
from collections import deque, OrderedDict
from dataclasses import dataclass, field
from array import array
import hashlib
import heapq
import math
import time

import numpy as np

from .mobility import engine_acceleration, rolling_acceleration
from .navigation_cache import NavigationCache, sample_coordinates


@dataclass
class Route:
    """Completed geometry, shared read-only with later searches."""
    path: tuple
    points: tuple
    remaining: tuple
    reverse_index: dict
    generation: int = 0
    cost_policy: bytes | None = None
    effort_remaining: tuple = ()


@dataclass
class Search:
    start: int
    goal: int
    generation: int
    frontier: list = field(default_factory=list)
    costs: dict = field(default_factory=dict)
    parents: dict = field(default_factory=dict)
    path: tuple = ()
    done: bool = False
    expanded: int = 0
    route: Route | None = None
    reuse: Route | None = None
    tracing: bool = False
    trace_node: int | None = None
    tail_at: int = -1
    tail_stop: int = -1
    nodes_back: list = field(default_factory=list)
    points_back: list = field(default_factory=list)
    remaining_back: list = field(default_factory=list)
    reverse_index: dict = field(default_factory=dict)
    previous_route: Route | None = None
    blocked: frozenset = field(default_factory=frozenset)
    climb_edges: np.ndarray | None = None
    effort_back: list = field(default_factory=list)
    climb_costs: bytes | None = None
    landmark_terms: tuple = ()


class Navigation:
    CELL = 2.
    MAX_CELLS = 262144
    EXPANSIONS_PER_STEP = 96
    MAX_SEARCH_NODES = 65536
    MAX_PATH_NODES = 16384
    MAX_CACHED_ROUTES = 32
    MAX_BLOCKED_NODES = 32
    COST_SCALE = 16
    MAX_UPHILL_COST = 255 / COST_SCALE
    JOIN_RINGS = 4
    MAX_ROUTE_JOIN_CHECKS = 8
    LANDMARK_SCALE = 256
    LANDMARK_INFINITY = 2**32-1
    LANDMARK_BATCH = 256
    MAX_LANDMARK_POLICIES = 16
    NEIGHBOURS = ((1,0,1.),(0,1,1.),(-1,0,1.),(0,-1,1.),
                  (1,1,math.sqrt(2)),(-1,1,math.sqrt(2)),
                  (-1,-1,math.sqrt(2)),(1,-1,math.sqrt(2)))
    _DIRECTION_BITS = {(dx,dz):1<<bit for bit,(dx,dz,_) in enumerate(NEIGHBOURS)}
    _DIRECTION_INDEX = {(dx,dz):bit for bit,(dx,dz,_) in enumerate(NEIGHBOURS)}

    @classmethod
    def dimensions(cls, bounds):
        x0,z0,x1,z1=bounds
        if not all(math.isfinite(v) for v in bounds) or x1<=x0 or z1<=z0:
            raise ValueError('Invalid navigation bounds')
        # Larger installed maps use a coarser grid instead of refusing a
        # previously supported battle or allocating an unbounded raster.
        cell=max(cls.CELL,math.ceil(math.sqrt((x1-x0)*(z1-z0)/cls.MAX_CELLS)*4)/4)
        return cell,math.floor((x1-x0)/cell),math.floor((z1-z0)/cell)

    def __init__(self, bounds, heights, blocked, *, cell=CELL, radius=0., slope=.4):
        self.bounds = tuple(bounds)
        self.cell, self.radius, self.slope = cell, radius, slope
        self.heights = np.asarray(heights, dtype=float)
        self.base = np.asarray(blocked, dtype=bool).copy()
        self.nz, self.nx = self.heights.shape
        if self.base.shape != self.heights.shape or self.nx*self.nz > self.MAX_CELLS:
            raise ValueError('Invalid navigation grid')
        self.counts = np.zeros(self.base.shape, dtype=np.uint16)
        self.walkable = ~self.base
        self.breakables = {}
        self.footprints = {}
        self.actor_slopes = {}
        # Retained for explicit legacy callers; an engine estimate is never
        # installed as a hard gate by build(). XML/static slope stays hard.
        self.actor_climb_edges = {}
        self.actor_climb_costs = {}
        self.removed = set()
        self.generation = 0
        self.searches = {}
        self.queue = deque()
        self.routes = OrderedDict()
        self._landmark_fields = {}
        self._edges = np.zeros(self.nx*self.nz,dtype=np.uint8)
        self._offsets = tuple(dz*self.nx+dx for dx,dz,_ in self.NEIGHBOURS)
        self._update_edges()
        self.stats = dict(expanded=0, max_expanded_per_step=0, requests=0, completed=0, failed=0,
                          path_work=0,max_work_per_step=0,reused=0,waypoint_probes=0)

    @classmethod
    def build(cls, world, entities, goals=None):
        """Incremental loading work. No mutation of Bullet or vehicle poses."""
        entities=tuple(entities)
        assets = [world.assets_for(e) for e in entities]
        # A disc enclosing the chassis permits turning at a waypoint without
        # clipping a long nose through the corner of a building.
        radius, height = 0., 0.
        footprints={}
        for entity,scene in zip(entities,assets):
            half_width=half_length=0.
            for name, vertices in scene.tank_shapes:
                if name == scene.turret_node:
                    continue
                low, high = vertices.min(0), vertices.max(0)
                half_width=max(half_width,float(max(abs(low[0]),abs(high[0]))))
                half_length=max(half_length,float(max(abs(low[2]),abs(high[2]))))
                radius = max(radius, math.hypot(max(abs(low[0]),abs(high[0])),
                                               max(abs(low[2]),abs(high[2]))))
                height = max(height, float(high[1])+1.)
            footprints[entity]=(half_width+.3,half_length+.3)
        # Humans are traffic obstacles too. Their dimensions do not enlarge
        # the NPC navigation disc, but must not fall back to that largest disc
        # as if it were their actual half-width and half-length.
        for entity in getattr(world,'poses',{}):
            if entity in footprints:continue
            scene=world.assets_for(entity);half_width=half_length=0.
            for name,vertices in scene.tank_shapes:
                if name==scene.turret_node:continue
                low,high=vertices.min(0),vertices.max(0)
                half_width=max(half_width,float(max(abs(low[0]),abs(high[0]))))
                half_length=max(half_length,float(max(abs(low[2]),abs(high[2]))))
            footprints[entity]=(half_width+.3,half_length+.3)
        radius += .3
        slope = min(s.max_slope for s in assets)*.85
        bounds = world.assets.bounds
        cell,nx,nz = cls.dimensions(bounds)
        radius=max(radius,cell*.72)
        if nx*nz > cls.MAX_CELLS or min(nx,nz) < 2:
            raise ValueError('Map exceeds navigation grid budget')

        started = time.monotonic()
        prepared = getattr(getattr(world.assets, 'mesh_files', None), 'prepared', None)
        cache = None
        if prepared is not None:
            from .collision import CollisionWorld
            from .terrain import MapTerrain
            if (getattr(world._terrain_heights, '__func__', None) is CollisionWorld._terrain_heights
                    and type(world.terrain) is MapTerrain
                    and getattr(world.terrain.height, '__func__', None) is MapTerrain.height):
                cache = NavigationCache(prepared)
        if cache is None:
            xs,zs = sample_coordinates(bounds, cell, nx, nz)
            heights = world._terrain_heights(xs.ravel(),zs.ravel()).reshape(nz,nx)
        else:
            heights = yield from cache.heights(bounds, cell, nx, nz, world._terrain_heights)
        grid_key = None
        build = lambda: cls._raster(world, bounds, cell, nx, nz, radius, height, slope, heights)
        if cache is None:
            raster = yield from build()
        else:
            grid_key = cache.key('raster', cell, nx, nz, radius, height, slope)
            raster = yield from cache.get(grid_key, build)
        nav = cls(bounds, heights, raster['base'], cell=cell, radius=radius, slope=slope)
        offsets, indices = raster['offsets'], raster['indices']
        for index, key in enumerate(raster['keys']):
            cells = indices[offsets[index]:offsets[index+1]]
            nav.breakables[int(key)] = cells
            nav.counts.ravel()[cells] += 1
        nav.walkable=~nav.base&(nav.counts==0)
        nav._update_edges()
        nav.footprints=footprints
        climb_costs={}
        for entity,scene in zip(entities,assets):
            angle=nav._configured_uphill_slope(getattr(scene,'definition',None))
            if angle is None:continue
            nav.actor_slopes[entity]=angle
            if angle<nav.slope:
                if angle not in climb_costs:
                    if cache is None:
                        climb_costs[angle]=nav._uphill_costs(angle)
                        yield
                    else:
                        def build_costs():
                            table = np.frombuffer(nav._uphill_costs(angle), dtype=np.uint8)
                            yield
                            return {'table': table}

                        key = cache.key('uphill', cell, nx, nz, angle)
                        arrays = yield from cache.get(key, build_costs)
                        climb_costs[angle] = arrays['table'].tobytes()
                nav.actor_climb_costs[entity]=climb_costs[angle]
        landmark_started = time.monotonic()
        poses=getattr(world,'poses',{})
        yield from nav._prepare_landmarks(((entity,getattr(poses.get(entity),'position',None))
                                         for entity in entities), cache=cache, grid_key=grid_key)
        route_started = time.monotonic()
        # Initial routes use the current entities, spawn positions and goals.
        for entity,goal in (goals or {}).items():
            nav.request(entity,world.poses[entity].position,goal)
            while nav.queue:
                nav.step()
                yield
        nav.build_timings = dict(nav_grid_ms=(landmark_started-started)*1000,
            nav_landmarks_ms=(route_started-landmark_started)*1000,
            nav_routes_ms=(time.monotonic()-route_started)*1000)
        if cache is not None:
            nav.build_timings.update(nav_cache_hits=cache.hits, nav_cache_misses=cache.misses)
        return nav

    @staticmethod
    def _raster(world, bounds, cell, nx, nz, radius, height, slope, heights):
        x0,z0,x1,z1 = bounds
        xs,zs = sample_coordinates(bounds, cell, nx, nz)
        blocked = np.zeros((nz,nx),bool)
        # Sample the complete footprint, not just the centre, at slopes/edges.
        for dx,dz in ((radius,0),(-radius,0),(0,radius),(0,-radius),
                      (radius*.707,radius*.707),(-radius*.707,radius*.707),
                      (-radius*.707,-radius*.707),(radius*.707,-radius*.707)):
            other = world._terrain_heights((xs+dx).ravel(),(zs+dz).ravel()).reshape(nz,nx)
            blocked |= np.abs(other-heights) > math.tan(slope)*math.hypot(dx,dz)
            yield
        blocked |= (xs<x0+radius)|(xs>x1-radius)|(zs<z0+radius)|(zs>z1-radius)
        keys, object_offsets, all_indices = [], [0], []
        objects = [(None,world.assets.vertices,world.assets.triangles)]
        objects += [(key,obj.vertices,obj.triangles) for key,obj in world.assets.destructibles.items()]
        for key,vertices,triangles in objects:
            marked = np.zeros((nz,nx),bool)
            for begin in range(0,len(triangles),256):
                faces = vertices[triangles[begin:begin+256]]
                # One chunk can include a very large roof; split again before
                # allocating repeated cell/triangle arrays.
                pending = [faces]
                while pending:
                    faces = pending.pop()
                    low,high = faces.min(1),faces.max(1)
                    ix0 = np.maximum(0,np.ceil((low[:,0]-radius-x0)/cell-.5).astype(int))
                    iz0 = np.maximum(0,np.ceil((low[:,2]-radius-z0)/cell-.5).astype(int))
                    ix1 = np.minimum(nx-1,np.floor((high[:,0]+radius-x0)/cell-.5).astype(int))
                    iz1 = np.minimum(nz-1,np.floor((high[:,2]+radius-z0)/cell-.5).astype(int))
                    widths = np.maximum(0,ix1-ix0+1)
                    counts = widths*np.maximum(0,iz1-iz0+1)
                    total = int(counts.sum())
                    if total > 131072 and len(faces)>1:
                        middle=len(faces)//2;pending += [faces[middle:],faces[:middle]]
                        continue
                    if total:
                        owners = np.repeat(np.arange(len(faces)),counts)
                        offsets = np.arange(total)-np.repeat(np.cumsum(counts)-counts,counts)
                        ix = ix0[owners]+offsets%widths[owners]
                        iz = iz0[owners]+offsets//widths[owners]
                        px,pz = x0+(ix+.5)*cell,z0+(iz+.5)*cell
                        selected = faces[owners]
                        distance2 = np.full(total,math.inf)
                        signs=[]
                        for a,b in ((0,1),(1,2),(2,0)):
                            ax,az = selected[:,a,0],selected[:,a,2]
                            dx,dz = selected[:,b,0]-ax,selected[:,b,2]-az
                            fraction=np.clip(((px-ax)*dx+(pz-az)*dz)/np.maximum(1e-20,dx*dx+dz*dz),0,1)
                            distance2=np.minimum(distance2,(px-ax-fraction*dx)**2+(pz-az-fraction*dz)**2)
                            signs.append(dx*(pz-az)-dz*(px-ax))
                        area=(selected[:,1,0]-selected[:,0,0])*(selected[:,2,2]-selected[:,0,2])-(selected[:,1,2]-selected[:,0,2])*(selected[:,2,0]-selected[:,0,0])
                        inside=((np.minimum.reduce(signs)>=-1e-8)|(np.maximum.reduce(signs)<=1e-8))&(np.abs(area)>1e-8)
                        covered=inside|(distance2<=radius*radius)
                        normal=np.cross(selected[:,1]-selected[:,0],selected[:,2]-selected[:,0])
                        horizontal=np.abs(normal[:,1])>np.hypot(normal[:,0],normal[:,2])
                        plane_y=selected[:,0,1]-(normal[:,0]*(px-selected[:,0,0])+normal[:,2]*(pz-selected[:,0,2]))/np.where(horizontal,normal[:,1],1.)
                        # Broad vertical faces use their height interval. A
                        # shallow surface uses its plane, avoiding false walls
                        # where a mesh follows an ordinary terrain slope.
                        bottom=np.where(horizontal,plane_y,low[owners,1])
                        top=np.where(horizontal,plane_y,high[owners,1])
                        ground=heights[iz,ix]
                        hit=covered&(top>ground+.28)&(bottom<ground+height)
                        marked[iz[hit],ix[hit]]=True
                    yield
            if key is None:
                blocked |= marked
            else:
                indices = np.flatnonzero(marked)
                keys.append(key)
                object_offsets.append(object_offsets[-1]+len(indices))
                all_indices.append(indices)
        return dict(base=blocked, keys=np.asarray(keys, dtype=np.int64),
                    offsets=np.asarray(object_offsets, dtype=np.intp),
                    indices=np.concatenate(all_indices) if all_indices else np.empty(0, dtype=np.intp))

    def _landmark_field(self, edges, seed, policy, costs, *, reverse=False):
        """Cooperative directed Dijkstra over permanent geometry only."""
        distance=array('I',[self.LANDMARK_INFINITY])*(self.nx*self.nz)
        if distance.itemsize!=4:raise RuntimeError('Navigation landmarks require uint32 arrays')
        distance[seed]=0
        frontier=[(0,seed)]
        opposites=tuple(self._DIRECTION_INDEX[(-dx,-dz)] for dx,dz,_ in self.NEIGHBOURS)
        work=0
        while frontier:
            cost,node=heapq.heappop(frontier)
            if distance[node]==cost:
                mask=int(edges[node])
                for bit,offset in enumerate(self._offsets):
                    if not mask&(1<<bit):continue
                    other=node+offset
                    value=self.COST_SCALE
                    if policy is not None:
                        origin=other if reverse else node
                        direction=opposites[bit] if reverse else bit
                        value=policy[origin*8+direction]
                    candidate=cost+costs[bit][value]
                    if candidate<distance[other]:
                        distance[other]=candidate
                        heapq.heappush(frontier,(candidate,other))
            work+=1
            if work>=self.LANDMARK_BATCH:
                yield
                work=0
        if work:yield
        return distance

    def _prepare_landmarks(self, spawns, *, cache=None, grid_key=None):
        """Publish complete lower-bound fields before any initial search."""
        if self.searches:raise RuntimeError('Prepare navigation landmarks before searches')
        spawns=tuple(spawns)
        if not any(position is not None for _,position in spawns):return
        # Breakables can disappear. Their initial barriers must never inflate
        # a lower bound used after destruction has opened a shortcut.
        permanent=Navigation(self.bounds,self.heights,self.base,cell=self.cell,
                             radius=self.radius,slope=self.slope)
        yield
        seed=None
        for _,position in spawns:
            if position is not None:
                seed=permanent.nearest(position)
                if seed is not None:break
        if seed is None:return
        costs=tuple(tuple(int(length*self.LANDMARK_SCALE*value/self.COST_SCALE)
                          for value in range(256)) for _,_,length in self.NEIGHBOURS)
        def build_seeds():
            unweighted = yield from self._landmark_field(permanent._edges, seed, None, costs)
            farthest = seed
            for begin in range(0,len(unweighted),self.LANDMARK_BATCH):
                for node in range(begin,min(begin+self.LANDMARK_BATCH,len(unweighted))):
                    value=unweighted[node]
                    if value!=self.LANDMARK_INFINITY and value>unweighted[farthest]:
                        farthest=node
                yield
            seeds = (seed,farthest) if farthest!=seed else (seed,)
            return dict(seeds=np.asarray(seeds, dtype=np.intp), forward=np.asarray(unweighted))

        if cache is None:
            anchors = yield from build_seeds()
        else:
            anchors = yield from cache.get(cache.key('anchors', grid_key, seed), build_seeds)
        seeds = tuple(map(int, anchors['seeds']))
        unweighted = self._field_from_array(anchors['forward'])
        policies=tuple(dict.fromkeys(self.actor_climb_costs.get(entity) for entity,_ in spawns))
        prepared={}
        for policy in policies[:self.MAX_LANDMARK_POLICIES]:
            if cache is None:
                fields = yield from self._policy_landmarks(permanent._edges, seeds, policy, costs, unweighted)
            else:
                policy_key = None if policy is None else hashlib.sha256(policy).hexdigest()

                def build_policy():
                    fields = yield from self._policy_landmarks(permanent._edges, seeds, policy, costs, unweighted)
                    # An undirected field uses the same distances in both directions.
                    return {'fields': np.asarray([pair[:1] if policy is None else pair for pair in fields])}

                key = cache.key('policy', grid_key, seed, policy_key)
                arrays = yield from cache.get(key, build_policy)
                fields = []
                for pair in arrays['fields']:
                    forward = self._field_from_array(pair[0])
                    reverse = forward if policy is None else self._field_from_array(pair[1])
                    fields.append((forward, reverse))
            prepared[policy]=tuple(fields)
        if self.searches:raise RuntimeError('Navigation search started during landmark preparation')
        self._landmark_fields=prepared

    @staticmethod
    def _field_from_array(values):
        field = array('I')
        field.frombytes(np.asarray(values, dtype=np.uint32).tobytes())
        return field

    def _policy_landmarks(self, edges, seeds, policy, costs, unweighted):
        fields = []
        for landmark in seeds:
            if policy is None and landmark == seeds[0]:
                forward = unweighted
            else:
                forward = yield from self._landmark_field(edges, landmark, policy, costs)
            if policy is None:
                reverse = forward
            else:
                reverse = yield from self._landmark_field(edges, landmark, policy, costs, reverse=True)
            fields.append((forward, reverse))
        return fields

    def refresh(self, destroyed):
        changed=sorted(set(destroyed)-self.removed)
        touched=[]
        for key in changed:
            indices=self.breakables.get(key)
            if indices is not None:
                self.counts.ravel()[indices]-=1
                if len(indices):touched.append(indices)
            self.removed.add(key)
        if not touched:return
        cells=np.unique(np.concatenate(touched))
        flat=self.walkable.ravel()
        before=flat[cells]
        flat[cells]=~self.base.ravel()[cells]&(self.counts.ravel()[cells]==0)
        opened=cells[flat[cells]&~before]
        if not len(opened):return
        # Destruction only opens cells: every existing route remains valid.
        # Finish an in-flight search on this conservative graph instead of
        # losing its entire frontier whenever another prop is destroyed.
        previous = self.generation
        self.generation+=1
        for search in self.searches.values():
            if not search.done and search.generation == previous:
                search.generation = self.generation
        # Completed routes keep their generation so later requests can find
        # newly opened shortcuts while following the existing valid route.
        z,x=np.divmod(opened,self.nx);neighbours=[]
        for dz in (-1,0,1):
            for dx in (-1,0,1):
                xx,zz=x+dx,z+dz
                valid=(xx>=0)&(xx<self.nx)&(zz>=0)&(zz<self.nz)
                neighbours.append(zz[valid]*self.nx+xx[valid])
        self._update_edges(np.unique(np.concatenate(neighbours)))

    def _update_edges(self, nodes=None):
        """Cache the same static edge gates used by traversable().

        Initial work belongs to loading. A destruction touches only affected
        cells and their neighbours, not a full-map rebuild in a battle tick.
        """
        nodes=np.arange(self.nx*self.nz) if nodes is None else nodes
        z,x=np.divmod(nodes,self.nx)
        flat=self.walkable.ravel();height=self.heights.ravel()
        masks=np.zeros(len(nodes),dtype=np.uint8)
        limit=math.tan(self.slope)*self.cell
        for bit,(dx,dz,length) in enumerate(self.NEIGHBOURS):
            xx,zz=x+dx,z+dz
            valid=(xx>=0)&(xx<self.nx)&(zz>=0)&(zz<self.nz)
            xx=np.clip(xx,0,self.nx-1);zz=np.clip(zz,0,self.nz-1)
            target=zz*self.nx+xx
            allowed=valid&flat[nodes]&flat[target]&(np.abs(height[target]-height[nodes])<=limit*length)
            if dx and dz:
                horizontal=z*self.nx+xx;vertical=zz*self.nx+x
                allowed &= (flat[horizontal]&flat[vertical]
                    &(np.abs(height[horizontal]-height[nodes])<=limit)
                    &(np.abs(height[vertical]-height[nodes])<=limit)
                    &(np.abs(height[target]-height[horizontal])<=limit)
                    &(np.abs(height[target]-height[vertical])<=limit))
            masks |= allowed.astype(np.uint8)<<bit
        self._edges[nodes]=masks

    def _configured_uphill_slope(self, definition):
        """A healthy at-rest traction estimate, not a moving tank's hard limit."""
        try:
            power=definition.drive.engine_power_hp
            mass=definition.mass
            resistance=definition.drive.terrain_resistance
            if len(resistance)!=3:return None
            values=(power,mass,*resistance)
            if any(isinstance(value,bool) or not isinstance(value,(int,float))
                   or not math.isfinite(value) or value<=0 for value in values):
                return None
            acceleration=engine_acceleration(power*735.49875,mass,0.,resistance[1])
            rolling=rolling_acceleration(resistance[1],0.)
        except (AttributeError,TypeError,ValueError,OverflowError):
            return None
        return min(self.slope,.85*math.asin(max(.01,min(1.,(acceleration-rolling)/9.81))))

    def _uphill_costs(self, angle):
        """Loading-only multipliers in sixteenths, eight immutable bytes per cell.

        Normalised launch time scales as 1/sqrt(net traction). The configured
        angle includes the existing healthy drivetrain safety margin. Once
        gravity exhausts that reserve, saturate at the byte format's finite
        maximum: momentum can still carry a tank over a short steep edge.
        Downhill has no engine penalty.
        """
        nodes=np.arange(self.nx*self.nz)
        z,x=np.divmod(nodes,self.nx)
        height=self.heights.ravel()
        costs=np.empty((len(nodes),8),dtype=np.uint8)
        limit=math.tan(angle)*self.cell
        for bit,(dx,dz,length) in enumerate(self.NEIGHBOURS):
            xx=np.clip(x+dx,0,self.nx-1);zz=np.clip(z+dz,0,self.nz-1)
            target=zz*self.nx+xx
            effort=(height[target]-height[nodes])/(limit*length)
            if dx and dz:
                horizontal=z*self.nx+xx;vertical=zz*self.nx+x
                effort=np.maximum.reduce((effort,
                    (height[horizontal]-height[nodes])/limit,
                    (height[vertical]-height[nodes])/limit,
                    (height[target]-height[horizontal])/limit,
                    (height[target]-height[vertical])/limit))
            reserve = np.maximum(1. / self.MAX_UPHILL_COST ** 2,
                                 1. - np.maximum(0., effort))
            effort = 1. / np.sqrt(reserve)
            costs[:,bit]=np.rint(effort*self.COST_SCALE).astype(np.uint8)
        return costs.tobytes()

    def _uphill_edges(self, angle):
        """Loading-only height gates, independent of live destructible cells."""
        nodes=np.arange(self.nx*self.nz)
        z,x=np.divmod(nodes,self.nx)
        height=self.heights.ravel()
        masks=np.zeros(len(nodes),dtype=np.uint8)
        limit=math.tan(angle)*self.cell
        for bit,(dx,dz,length) in enumerate(self.NEIGHBOURS):
            xx,zz=x+dx,z+dz
            valid=(xx>=0)&(xx<self.nx)&(zz>=0)&(zz<self.nz)
            xx=np.clip(xx,0,self.nx-1);zz=np.clip(zz,0,self.nz-1)
            target=zz*self.nx+xx
            allowed=valid&(height[target]-height[nodes]<=limit*length)
            if dx and dz:
                horizontal=z*self.nx+xx;vertical=zz*self.nx+x
                allowed &= ((height[horizontal]-height[nodes]<=limit)
                    &(height[vertical]-height[nodes]<=limit)
                    &(height[target]-height[horizontal]<=limit)
                    &(height[target]-height[vertical]<=limit))
            masks |= allowed.astype(np.uint8)<<bit
        masks.flags.writeable=False
        return masks

    def point(self, node):
        z,x=divmod(node,self.nx)
        return (self.bounds[0]+(x+.5)*self.cell,float(self.heights[z,x]),
                self.bounds[1]+(z+.5)*self.cell)

    def node(self, position):
        x=math.floor((position[0]-self.bounds[0])/self.cell)
        z=math.floor((position[2]-self.bounds[1])/self.cell)
        return z*self.nx+x if 0<=x<self.nx and 0<=z<self.nz else None

    def nearest(self, position, rings=6, *, blocked=()):
        node=self.node(position)
        if node is None:return None
        z,x=divmod(node,self.nx)
        for r in range(rings+1):
            best=None
            for dz in range(-r,r+1):
                for dx in range(-r,r+1):
                    if max(abs(dx),abs(dz))!=r:continue
                    xx,zz=x+dx,z+dz
                    if 0<=xx<self.nx and 0<=zz<self.nz and self.walkable[zz,xx]:
                        candidate=zz*self.nx+xx
                        if candidate in blocked:continue
                        rank=((self.point(candidate)[0]-position[0])**2+(self.point(candidate)[2]-position[2])**2,candidate)
                        if best is None or rank<best:best=rank
            if best is not None:return best[1]
        return None

    def traversable(self, a, b, *, blocked=(), climb_edges=None):
        az,ax=divmod(a,self.nx);bz,bx=divmod(b,self.nx)
        if a in blocked or b in blocked or not self.walkable[bz,bx]:return False
        dx,dz=abs(bx-ax),abs(bz-az)
        if max(dx,dz)!=1:return False
        if climb_edges is not None and not int(climb_edges[a])&self._DIRECTION_BITS[(bx-ax,bz-az)]:
            return False
        if dx and dz:
            if az*self.nx+bx in blocked or bz*self.nx+ax in blocked:return False
            if not self.walkable[az,bx] or not self.walkable[bz,ax]:return False
            limit=math.tan(self.slope)*self.cell
            if (abs(self.heights[az,bx]-self.heights[az,ax])>limit or
                    abs(self.heights[bz,ax]-self.heights[az,ax])>limit or
                    abs(self.heights[bz,bx]-self.heights[az,bx])>limit or
                    abs(self.heights[bz,bx]-self.heights[bz,ax])>limit):return False
        return abs(self.heights[bz,bx]-self.heights[az,ax]) <= math.tan(self.slope)*self.cell*math.hypot(dx,dz)

    def clear(self, start, end, *, blocked=(), climb_edges=None):
        """Supercover via adjacent grid transitions, including corner gates."""
        return self._segment_effort(start,end,blocked=blocked,climb_edges=climb_edges) is not None

    def _segment_effort(self, start, end, *, blocked=(), climb_edges=None, climb_costs=None,
                        allow_blocked_start=False):
        """Validate one short corridor and optionally count its tabled effort."""
        distance=math.hypot(end[0]-start[0],end[2]-start[2])
        count=max(1,math.ceil(distance/(self.cell*.25)))
        previous=self.node(start)
        if previous is None or not self.walkable.ravel()[previous]:return None
        escaping=allow_blocked_start and previous in blocked
        if previous in blocked and not escaping:return None
        if escaping:
            target=self.node(end)
            if target is None or target in blocked:return None
        effort=0.
        if climb_costs is not None:
            dx,dz=end[0]-start[0],end[2]-start[2]
            direction=(1 if dx>0 else -1 if dx<0 else 0,
                       1 if dz>0 else -1 if dz<0 else 0)
            bit=self._DIRECTION_INDEX[direction] if distance else 0
            sample=distance/count/self.COST_SCALE
        for i in range(1,count+1):
            p=(start[0]+(end[0]-start[0])*i/count,0.,start[2]+(end[2]-start[2])*i/count)
            node=self.node(p)
            if node is None:return None
            if node!=previous:
                # A learned ban may cover the chassis itself. Permit only its
                # contiguous exit; static cells, slopes and hard masks remain.
                if not self.traversable(previous,node,blocked=() if escaping else blocked,
                                        climb_edges=climb_edges):return None
                if escaping and node not in blocked:escaping=False
            if climb_costs is not None:
                effort+=sample*climb_costs[previous*8+bit]
            previous=node
        return effort

    def _blocked_snapshot(self, nodes):
        """Bound input work as well as the per-NPC learned exclusion set."""
        try:iterator=iter(nodes)
        except TypeError:raise ValueError('Invalid learned navigation cells') from None
        result=set()
        for count,node in enumerate(iterator):
            if count>=self.MAX_BLOCKED_NODES:
                raise ValueError('Too many learned navigation cells')
            if type(node) is not int or not 0<=node<self.nx*self.nz:
                raise ValueError('Invalid learned navigation cell')
            result.add(node)
        return frozenset(result)

    def request(self, entity, start, goal, *, blocked=(), restart=False):
        """Plan around one NPC's bounded snapshot; expiry belongs to its AI.

        Explicit restart reconnects from the live position to a compatible tail.
        """
        blocked=self._blocked_snapshot(blocked)
        a,b=self.nearest(start,blocked=blocked),self.nearest(goal,blocked=blocked)
        if a is None or b is None:
            self.cancel(entity)
            return None
        climb_edges=self.actor_climb_edges.get(entity)
        climb_costs=self.actor_climb_costs.get(entity)
        old=self.searches.get(entity)
        if (not restart and old is not None and old.goal==b and old.generation==self.generation
                and old.blocked==blocked and old.climb_edges is climb_edges
                and old.climb_costs is climb_costs):
            return old
        fields=getattr(self,'_landmark_fields',{}).get(climb_costs,())
        terms=tuple((forward,reverse,forward[b],reverse[b]) for forward,reverse in fields)
        search=Search(a,b,self.generation,[(self._estimate(a,b,terms),0.,a)],{a:0.},
                      blocked=blocked,climb_edges=climb_edges,climb_costs=climb_costs,
                      landmark_terms=terms)
        previous_tail=None
        if (old is not None and old.goal==b and old.route is not None
                and old.climb_edges is climb_edges and old.climb_costs is climb_costs):
            # Keep driving the valid completed route while optimising it.
            # A new Search owns all mutable reconstruction state; clearing
            # the old reverse_index would also corrupt its immutable Route.
            fallback=old.route
            if (old.previous_route is not None and old.previous_route.path
                    and old.previous_route.path[-1]==b):
                fallback=old.previous_route
            # At most32 indexed probes, never a whole-path scan in a tick.
            if (fallback.path and fallback.path[-1]==b
                    and not any(node in fallback.reverse_index for node in blocked)):
                if restart:
                    # The caller could not join this route. Reconnect through
                    # budgeted A*, without advertising it as a drivable path.
                    # Learned corner bans need a fresh search, not a reused tail.
                    if not blocked:previous_tail=fallback
                else:
                    search.route=fallback
                    search.path=fallback.path
        elif (old is not None and old.goal!=b and old.climb_edges is climb_edges
                and old.climb_costs is climb_costs):
            # A new task can use a short part of the old course while A* runs.
            # Its endpoint stays distinct and every interim join is validated.
            search.previous_route=old.route if old.route is not None else old.previous_route
        # Capture one immutable tail for this search. Switching tails while
        # reconstructing an existing parent chain could introduce a cycle.
        cached=self.routes.get(b)
        # A cached tail is a valid compatible route, not an optimality proof.
        search.reuse=previous_tail
        if (search.reuse is None and not blocked and climb_edges is None and cached is not None
                and cached.path and cached.path[-1]==b and cached.generation==self.generation
                and cached.cost_policy is climb_costs):
            search.reuse=cached
        self.searches[entity]=search
        if entity not in self.queue:self.queue.append(entity)
        self.stats['requests']+=1
        return search

    def cancel(self, entity):
        self.searches.pop(entity,None)
        try:self.queue.remove(entity)
        except ValueError:pass

    def heuristic(self, a, b):
        az,ax=divmod(a,self.nx);bz,bx=divmod(b,self.nx)
        dx,dz=abs(ax-bx),abs(az-bz)
        return max(dx,dz)+(math.sqrt(2)-1)*min(dx,dz)

    def _estimate(self, node, goal, terms=()):
        value=self.heuristic(node,goal)
        for forward,reverse,from_goal,to_goal in terms:
            from_node=forward[node]
            if from_goal!=self.LANDMARK_INFINITY and from_node!=self.LANDMARK_INFINITY:
                value=max(value,(from_goal-from_node)/self.LANDMARK_SCALE)
            to_node=reverse[node]
            if to_goal!=self.LANDMARK_INFINITY and to_node!=self.LANDMARK_INFINITY:
                value=max(value,(to_node-to_goal)/self.LANDMARK_SCALE)
        return value

    def step(self, budget=EXPANSIONS_PER_STEP):
        expanded=work=0
        # Count heap pops too: stale entries must not evade the frame budget.
        while self.queue and work<budget:
            entity=self.queue.popleft();s=self.searches.get(entity)
            if s is None or s.done:continue
            if s.generation!=self.generation:
                # Leave a completed fallback available until the director
                # requests a fresh search from the vehicle's live position.
                continue
            if s.tracing:
                # Reconstruct/cache one vertex per work unit. Long paths do
                # not add an unbudgeted Python loop to their completion tick.
                work+=1;self.stats['path_work']+=1
                if len(s.nodes_back)>=self.MAX_PATH_NODES:
                    s.done=True;s.tracing=False;s.reuse=None
                    s.frontier.clear();s.costs.clear();s.parents.clear()
                    s.nodes_back.clear();s.points_back.clear();s.remaining_back.clear();s.reverse_index.clear()
                    s.effort_back.clear()
                    self.stats['failed']+=1
                    continue
                if s.tail_at>s.tail_stop:
                    node=s.reuse.path[s.tail_at]
                    point=s.reuse.points[s.tail_at]
                    remaining=s.reuse.remaining[s.tail_at]
                    effort=s.reuse.effort_remaining[s.tail_at] if s.climb_costs is not None else 0.
                    s.tail_at-=1
                else:
                    node=s.trace_node;point=self.point(node)
                    distance=(math.hypot(point[0]-s.points_back[-1][0],point[2]-s.points_back[-1][2])
                              if s.points_back else 0.)
                    remaining=s.remaining_back[-1]+distance if s.points_back else 0.
                    effort=0.
                    if s.climb_costs is not None and s.nodes_back:
                        az,ax=divmod(node,self.nx);bz,bx=divmod(s.nodes_back[-1],self.nx)
                        bit=self._DIRECTION_INDEX[(bx-ax,bz-az)]
                        effort=s.effort_back[-1]+distance*s.climb_costs[node*8+bit]/self.COST_SCALE
                    s.trace_node=s.parents.get(node)
                s.reverse_index[node]=len(s.nodes_back)
                s.nodes_back.append(node);s.points_back.append(point);s.remaining_back.append(remaining)
                if s.climb_costs is not None:s.effort_back.append(effort)
                if node==s.start:
                    s.path=tuple(reversed(s.nodes_back))
                    # The vehicle may have travelled far along the old
                    # detour while this shortcut was being searched. Retire
                    # that one useful fallback only after a successful join.
                    if s.route is not None:s.previous_route=s.route
                    s.route=Route(s.path,tuple(reversed(s.points_back)),tuple(reversed(s.remaining_back)),
                        s.reverse_index,s.generation,s.climb_costs,tuple(reversed(s.effort_back)))
                    s.done=True;s.tracing=False;s.reuse=None
                    s.frontier.clear();s.costs.clear();s.parents.clear()
                    s.nodes_back.clear();s.points_back.clear();s.remaining_back.clear()
                    s.effort_back.clear()
                    self.routes[s.goal]=s.route;self.routes.move_to_end(s.goal)
                    if len(self.routes)>self.MAX_CACHED_ROUTES:self.routes.popitem(last=False)
                    self.stats['completed']+=1
                else:self.queue.append(entity)
                continue
            if not s.frontier or s.expanded>=self.MAX_SEARCH_NODES:
                s.done=True;s.frontier.clear();s.costs.clear();s.parents.clear()
                self.stats['failed']+=1;continue
            _,cost,node=heapq.heappop(s.frontier)
            expanded+=1;work+=1;s.expanded+=1
            if cost!=s.costs[node]:
                self.queue.append(entity);continue
            reused=s.reuse is not None and node in s.reuse.reverse_index
            if node==s.goal or reused:
                s.tracing=True;s.trace_node=node
                if reused and node!=s.goal:
                    s.tail_at=len(s.reuse.path)-1
                    s.tail_stop=len(s.reuse.path)-1-s.reuse.reverse_index[node]
                    self.stats['reused']+=1
                self.queue.append(entity)
                continue
            mask=int(self._edges[node])
            if s.climb_edges is not None:mask &= int(s.climb_edges[node])
            for bit,(dx,dz,length) in enumerate(self.NEIGHBOURS):
                if not mask&(1<<bit):continue
                other=node+self._offsets[bit]
                if s.blocked and (other in s.blocked or dx and dz and
                        (node+dx in s.blocked or node+dz*self.nx in s.blocked)):
                    continue
                multiplier=s.climb_costs[node*8+bit]/self.COST_SCALE if s.climb_costs is not None else 1.
                new=cost+length*multiplier
                if new>=s.costs.get(other,math.inf):continue
                if other not in s.costs and len(s.costs)>=self.MAX_SEARCH_NODES:continue
                s.costs[other]=new;s.parents[other]=node
                heapq.heappush(s.frontier,(new+self._estimate(other,s.goal,s.landmark_terms),new,other))
            self.queue.append(entity)
        self.stats['expanded']+=expanded
        self.stats['max_expanded_per_step']=max(self.stats['max_expanded_per_step'],expanded)
        self.stats['max_work_per_step']=max(self.stats['max_work_per_step'],work)
        return work

    def _nearest_route_index(self, route, position):
        node=self.node(position)
        if node is None:return None
        z,x=divmod(node,self.nx);best=None
        # A route point is a cell centre. Once the closest candidate lies
        # inside the distance to all unvisited rings, it is the exact nearest
        # point (including the original first-index tie break).
        for r in range(self.JOIN_RINGS+1):
            for dz in range(-r,r+1):
                for dx in range(-r,r+1):
                    if max(abs(dx),abs(dz))!=r:continue
                    xx,zz=x+dx,z+dz
                    if not (0<=xx<self.nx and 0<=zz<self.nz):continue
                    self.stats['waypoint_probes']+=1
                    reverse=route.reverse_index.get(zz*self.nx+xx)
                    if reverse is None:continue
                    index=len(route.path)-1-reverse;point=route.points[index]
                    rank=((position[0]-point[0])**2+(position[2]-point[2])**2,index)
                    if best is None or rank<best:best=rank
            left=self.bounds[0]+(x-r-.5)*self.cell
            right=self.bounds[0]+(x+r+1.5)*self.cell
            low=self.bounds[1]+(z-r-.5)*self.cell
            high=self.bounds[1]+(z+r+1.5)*self.cell
            distance=min(position[0]-left,right-position[0],position[2]-low,high-position[2])
            if best is not None and best[0]<distance*distance:return best[1]
        return None

    def waypoint(self, search, position, speed):
        if not search or search.done and not search.path:return None
        if search.path:
            following=self._route_waypoint(search.route,position,speed,
                blocked=search.blocked,climb_edges=search.climb_edges,climb_costs=search.climb_costs)
            if following is not None:
                search.previous_route=None
                return following
        if search.previous_route is not None:
            following=self._route_waypoint(search.previous_route,position,speed,
                blocked=search.blocked,climb_edges=search.climb_edges,climb_costs=search.climb_costs)
            if following is None:return None
            if search.previous_route.path[-1]!=search.goal:
                point,_=following
                goal=self.point(search.goal)
                distance=math.hypot(goal[0]-position[0],goal[2]-position[2])
                after=math.hypot(goal[0]-point[0],goal[2]-point[2])
                if after>=distance-1e-6:return None
                # The old endpoint can be nearby while the new task is far.
                # Brake against the new horizontal range during this handoff.
                return point,distance
            return following
        return None

    def _reachable_route_join(self, route, position, rejected, *, blocked=(),
                              climb_edges=None, climb_costs=None):
        """Try nearby route cells only after the closest join is obstructed."""
        node=self.node(position)
        z,x=divmod(node,self.nx)
        candidates=[]
        for zz in range(max(0,z-self.JOIN_RINGS),min(self.nz,z+self.JOIN_RINGS+1)):
            for xx in range(max(0,x-self.JOIN_RINGS),min(self.nx,x+self.JOIN_RINGS+1)):
                self.stats['waypoint_probes']+=1
                reverse=route.reverse_index.get(zz*self.nx+xx)
                if reverse is None:continue
                index=len(route.path)-1-reverse
                if index==rejected:continue
                point=route.points[index]
                distance2=(position[0]-point[0])**2+(position[2]-point[2])**2
                candidates.append((distance2,index))
        for _,index in sorted(candidates)[:self.MAX_ROUTE_JOIN_CHECKS]:
            effort=self._segment_effort(position,route.points[index],blocked=blocked,
                climb_edges=climb_edges,climb_costs=climb_costs,allow_blocked_start=True)
            if effort is not None:return index,effort
        return None

    def _route_waypoint(self, route, position, speed, *, blocked=(), climb_edges=None, climb_costs=None):
        points=route.points
        if self.node(position) in blocked:
            # Complete the selected nearby exit before looking farther along
            # the route. A shortcut across the initial ban can retry the slope.
            entry=points[0]
            distance=math.hypot(entry[0]-position[0],entry[2]-position[2])
            if route.path[0] in blocked or distance>self.cell*self.JOIN_RINGS:return None
            if self._segment_effort(position,entry,blocked=blocked,climb_edges=climb_edges,
                                    allow_blocked_start=True) is None:return None
            if climb_costs is not None and (route.cost_policy is not climb_costs or not route.effort_remaining):
                return None
            return entry,distance+route.remaining[0]
        # Starting index follows actual progress; a blocked segment is never
        # silently skipped after a ram or a recovery moved the vehicle.
        if climb_costs is not None and (route.cost_policy is not climb_costs or not route.effort_remaining):
            return None
        nearest=self._nearest_route_index(route,position)
        if nearest is None:return None
        join=self._segment_effort(position,points[nearest],blocked=blocked,
                                  climb_edges=climb_edges,climb_costs=climb_costs,
                                  allow_blocked_start=True)
        if join is None:
            reachable=self._reachable_route_join(route,position,nearest,blocked=blocked,
                climb_edges=climb_edges,climb_costs=climb_costs)
            if reachable is None:return None
            nearest,join=reachable
        index=nearest
        lookahead=max(5.,abs(speed)*.45)
        for i in range(nearest+1,min(len(points),nearest+12)):
            effort=self._segment_effort(position,points[i],blocked=blocked,
                                        climb_edges=climb_edges,climb_costs=climb_costs,
                                        allow_blocked_start=True)
            if effort is None:break
            if climb_costs is not None:
                along=join+route.effort_remaining[nearest]-route.effort_remaining[i]
                # Always permit the first actual route edge. Off-centre
                # steering may sample a different local slope direction;
                # rejecting this edge strands the tank at a cell centre.
                # Cost only governs shortcuts across subsequent route edges.
                if i>nearest+1 and effort>along+1e-6:break
            index=i
            if math.hypot(points[i][0]-position[0],points[i][2]-position[2])>=lookahead:break
        remaining=math.hypot(points[index][0]-position[0],points[index][2]-position[2])
        remaining+=route.remaining[index]
        return points[index],remaining
