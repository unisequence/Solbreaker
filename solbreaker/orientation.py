"""BigWorld's Y-up yaw, pitch and roll convention."""
import math
import numpy as np


def matrix(yaw, pitch=0., roll=0.):
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    return np.array(((cy*cr+sy*sp*sr, -cy*sr+sy*sp*cr, sy*cp),
                     (cp*sr, cp*cr, -sp),
                     (-sy*cr+cy*sp*sr, sy*sr+cy*sp*cr, cy*cp)))


def quaternion(yaw, pitch=0., roll=0.):
    cy, sy = math.cos(yaw/2), math.sin(yaw/2)
    cp, sp = math.cos(pitch/2), math.sin(pitch/2)
    cr, sr = math.cos(roll/2), math.sin(roll/2)
    return (cy*sp*cr+sy*cp*sr, sy*cp*cr-cy*sp*sr,
            cy*cp*sr-sy*sp*cr, cy*cp*cr+sy*sp*sr)
