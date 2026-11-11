# cython: language_level=3, binding=True, infer_types=False
from cpython.buffer cimport Py_buffer,PyObject_GetBuffer,PyBuffer_Release,PyBUF_SIMPLE
from libc.math cimport isfinite
cdef extern from "numpy/arrayobject.h":
    ctypedef struct PyArrayObject:
        pass
    ctypedef Py_ssize_t npy_intp
    enum:
        NPY_DOUBLE
    int PyArray_CheckExact(object)
    int PyArray_NDIM(PyArrayObject*)
    int PyArray_TYPE(PyArrayObject*)
    int PyArray_ISNOTSWAPPED(PyArrayObject*)
    int PyArray_ISCONTIGUOUS(PyArrayObject*)
    int PyArray_ISALIGNED(PyArrayObject*)
    npy_intp* PyArray_DIMS(PyArrayObject*)
    npy_intp PyArray_SIZE(PyArrayObject*)
    void* PyArray_DATA(PyArrayObject*)
    int _import_array() except -1

cdef struct SupportOutput:
    double plane[3]
    unsigned int active[3]
    unsigned int active_count
    unsigned int supported
    double* lower

_import_array()

def output_layout():
    cdef SupportOutput value
    cdef size_t base=<size_t>&value
    return (sizeof(SupportOutput),(<size_t>&value.plane-base,<size_t>&value.active-base,
        <size_t>&value.active_count-base,<size_t>&value.supported-base,<size_t>&value.lower-base))

def valid_layout(a,b,p,weight,Py_ssize_t count):
    """None means retain the original Python protocol for custom arrays."""
    cdef object value
    cdef PyArrayObject* array
    for value in (a,b,p,weight):
        if not PyArray_CheckExact(value):return None
        array=<PyArrayObject*>value
        if (PyArray_NDIM(array)!=1 or PyArray_DIMS(array)[0]!=count
                or PyArray_TYPE(array)!=NPY_DOUBLE or not PyArray_ISNOTSWAPPED(array)):
            return False
    return True

def valid_output(output,unsigned int supported,lower):
    """Retain every bounds/finite check without a temporary Boolean array."""
    cdef PyArrayObject* array
    cdef Py_buffer view
    cdef SupportOutput* value
    cdef Py_ssize_t count,index
    cdef double* data
    if not PyArray_CheckExact(lower):return None
    array=<PyArrayObject*>lower
    if (PyArray_NDIM(array)!=1 or PyArray_TYPE(array)!=NPY_DOUBLE
            or not PyArray_ISNOTSWAPPED(array)):
        return None
    if not PyArray_ISCONTIGUOUS(array) or not PyArray_ISALIGNED(array):return None
    count=PyArray_SIZE(array)
    # The caller verifies the ctypes class and all offsets before this path.
    if PyObject_GetBuffer(output,&view,PyBUF_SIMPLE)!=0:return None
    try:
        if view.len!=sizeof(SupportOutput):return None
        value=<SupportOutput*>view.buf
        if value.active_count>3 or value.supported!=supported:return False
        for index in range(value.active_count):
            if value.active[index]>=count:return False
        for index in range(3):
            if not isfinite(value.plane[index]):return False
        data=<double*>PyArray_DATA(array)
        for index in range(count):
            if not isfinite(data[index]):return False
        return True
    finally:
        PyBuffer_Release(&view)
