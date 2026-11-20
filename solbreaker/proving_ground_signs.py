"""Physical wayfinding at the eight sector entrances (east, north, heading)."""
import math

SIGNS = (
    ('01','SERVICE',52.,35.,56.),
    ('02','FIRING RANGE',210.,-21.,125.),
    ('03','DEMOLITION',-241.,15.,-57.),
    ('04','STUNTS',248.,191.,99.),
    ('05','QUARRY',-222.,191.,-130.),
    ('06','AIRFIELD',45.,380.,48.),
    ('07','ARENA',197.,-201.,98.),
    ('08','TEST CIRCUIT',-219.,-180.,-154.),
)


def sign_meshes(height):
    objects = []
    for number,_,x,y,angle in SIGNS:
        facing = math.radians(angle)
        rx,ry = math.cos(facing),-math.sin(facing)
        z = height(x,y)
        def point(horizontal, level, depth=0.):
            return [x+rx*horizontal+ry*depth, y+ry*horizontal-rx*depth, z+level]
        corners = [point(-4.8,2.7),point(4.8,2.7),point(4.8,6.5),point(-4.8,6.5)]
        objects.append(dict(name='sb_wayfinding_'+number,sign=number,vertices=corners,
                            triangles=[[0,1,2],[0,2,3]],
                            color=[1.,1.,1.,1.],solid=False))
        back = [point(horizontal, level, -.08) for horizontal, level in
                ((-4.8, 2.7), (4.8, 2.7), (4.8, 6.5), (-4.8, 6.5))]
        objects.append(dict(name='sb_wayback_'+number,vertices=back,
                            triangles=[[2,1,0],[3,2,0]], color=[.24,.26,.25,1.],
                            solid=False))
        vertices,triangles = [],[]
        for offset in (-3.25,3.25):
            base = len(vertices)
            for level in (-.4,6.4):
                for side,depth in ((-.13,-.12),(.13,-.12),(.13,.12),(-.13,.12)):
                    vertices.append(point(offset+side,level,depth-.21))
            for side in range(4):
                other=(side+1)%4
                triangles.extend([[base+side,base+other,base+side+4],
                                  [base+other,base+other+4,base+side+4]])
        objects.append(dict(name='sb_waypost_'+number+'_steel',vertices=vertices,triangles=triangles,
                            color=[.26,.29,.28,1.]))
    return objects
