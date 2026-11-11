"""Shortcut for an explicit float(default NumPy vector norm)."""
from math import sqrt as _sqrt
import numpy as np
_DEFAULT_NORM=np.linalg.norm
_F64=np.dtype('float64')
_FAST=(type(_DEFAULT_NORM).__name__=='_ArrayFunctionDispatcher'
       and type(_DEFAULT_NORM).__module__.startswith('numpy'))

def vector_length(value):
    if (_FAST and np.linalg.norm is _DEFAULT_NORM
            and type(value) is np.ndarray and value.dtype==_F64
            and value.ndim==1 and value.flags.c_contiguous):
        return _sqrt(value.dot(value))
    return float(np.linalg.norm(value))
