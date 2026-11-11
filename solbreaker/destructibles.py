"""AreaDestructibles wire state recovered from the pinned native client."""
import math
import struct

from .account import variable16


def packed_array(values, fmt):
    count = len(values)
    prefix = bytes([count]) if count < 255 else b'\xff'+count.to_bytes(3,'little')
    return prefix+b''.join(struct.pack('<'+fmt,v) for v in values)


def fall_record(local_id, yaw, fall_angle=math.pi/2, impulse=0, column=False):
    if not 0 <= local_id <= 255 or not 0 <= impulse <= 3:
        raise ValueError('Destructible ID/impulse exceeds native packing')
    direction = round(((yaw+math.pi) % math.tau)*32/math.pi) & 63
    upper = local_id << 8 | direction << 2 | impulse
    if column:
        return upper
    angle = round(((fall_angle+math.pi) % math.tau)*65536/math.tau) & 65535
    return upper << 16 | angle


class DestructibleState:
    def __init__(self, assets, entity_base=300, alias_base=2):
        self.areas = {area:entity_base+i for i,area in enumerate(sorted({k>>16 for k in assets}))}
        if (type(alias_base) is not int or alias_base<0 or
                len(self.areas)>250 or alias_base+len(self.areas)>255):
            raise ValueError('Too many destructible areas for this training arena')
        self.alias_base = alias_base
        self.values = {area:{1:[],2:[],3:[]} for area in self.areas}
        self.dirty = set()

    def spawn_parts(self):
        result = []
        for alias,(area,entity) in enumerate(self.areas.items(),self.alias_base):
            # onEnterWorld derives the area key from floor(x/100), floor(z/100).
            pos = (((area>>8)-127+.5)*100,0,((area&255)-127+.5)*100)
            props = b'\x04\0\0'+b''.join(bytes([p])+packed_array(self.values[area][p],
                        'H' if p==2 else 'I') for p in (1,2,3))
            body = b'\0'+struct.pack('<IH6f',entity,3,*pos,0,0,0)+props
            result.append(b'\x0a'+struct.pack('<IB',entity,alias)+variable16(9,body))
        return tuple(result)

    def spawn(self):
        return b''.join(self.spawn_parts())

    def destroy(self, obj, yaw, fall_angle):
        area,local = obj.key>>16,obj.key&65535
        prop = 3 if obj.kind==8 else 2 if obj.kind==5 else 1
        value = fall_record(local,yaw,fall_angle,column=prop==2)
        self.values[area][prop].append(value)
        self.dirty.add((area,prop))

    def updates(self):
        result = bytearray()
        for area,prop in sorted(self.dirty):
            payload = packed_array(self.values[area][prop],'H' if prop==2 else 'I')
            # getPropertyStreamSize returns -1: VARIABLE8, not VARIABLE16.
            # Extended lengths are handled by the Mercury encoder.
            from .mercury import variable8
            result += b'\x12'+struct.pack('<I',self.areas[area])+variable8(158+prop,payload)
        self.dirty.clear()
        return bytes(result)
