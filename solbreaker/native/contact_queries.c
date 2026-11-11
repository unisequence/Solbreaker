#define PY_SSIZE_T_CLEAN
#define Py_LIMITED_API 0x030B0000
#include <Python.h>
#include <math.h>

/* Group the existing PyBullet queries without accessing its private ABI.
 * Keep the GIL, query ordering, Python set ordering and returned point tuples.
 * All geometric decisions remain with the installed PyBullet implementation.
 */
static PyObject *gather(PyObject *self, PyObject *args) {
    PyObject *bullet, *cid, *player, *skin_object, *maps, *vehicle_values;
    PyObject *observer, *entity;
    PyObject *joints_fn=NULL, *aabb_fn=NULL, *near_fn=NULL, *closest_fn=NULL;
    PyObject *kw=NULL, *call=NULL, *value=NULL, *bounds=NULL, *low=NULL, *high=NULL;
    PyObject *near=NULL, *vehicles=NULL, *bodies=NULL, *iterator=NULL, *item=NULL;
    PyObject *points=NULL, *body=NULL, *rows=NULL, *row_iter=NULL, *row=NULL;
    double lo[3]={INFINITY,INFINITY,INFINITY}, hi[3]={-INFINITY,-INFINITY,-INFINITY};
    double skin;
    long joints, link;
    if (!PyArg_ParseTuple(args,"OOOOOOOO:gather",&bullet,&cid,&player,&skin_object,
                          &maps,&vehicle_values,&observer,&entity)) return NULL;
    skin=PyFloat_AsDouble(skin_object);
    if (PyErr_Occurred()) return NULL;
    joints_fn=PyObject_GetAttrString(bullet,"getNumJoints"); if (!joints_fn) goto error;
    aabb_fn=PyObject_GetAttrString(bullet,"getAABB"); if (!aabb_fn) goto error;
    near_fn=PyObject_GetAttrString(bullet,"getOverlappingObjects"); if (!near_fn) goto error;
    closest_fn=PyObject_GetAttrString(bullet,"getClosestPoints"); if (!closest_fn) goto error;
    kw=Py_BuildValue("{s:O}","physicsClientId",cid); if (!kw) goto error;
    call=PyTuple_Pack(1,player); if (!call) goto error;
    value=PyObject_Call(joints_fn,call,kw); Py_CLEAR(call); if (!value) goto error;
    joints=PyLong_AsLong(value); Py_CLEAR(value); if (PyErr_Occurred()) goto error;
    for (link=-1;link<joints;link++) {
        call=Py_BuildValue("Ol",player,link); if (!call) goto error;
        bounds=PyObject_Call(aabb_fn,call,kw); Py_CLEAR(call); if (!bounds) goto error;
        for (int axis=0;axis<3;axis++) {
            PyObject *a=PyTuple_GetItem(bounds,0), *b=PyTuple_GetItem(bounds,1);
            PyObject *av, *bv;
            double x,y;
            if (!a || !b) goto error;
            av=PyTuple_GetItem(a,axis); bv=PyTuple_GetItem(b,axis);
            if (!av || !bv) goto error;
            x=PyFloat_AsDouble(av); y=PyFloat_AsDouble(bv);
            if (PyErr_Occurred()) goto error;
            if (x<lo[axis]) lo[axis]=x;
            if (y>hi[axis]) hi[axis]=y;
        }
        Py_CLEAR(bounds);
    }
    low=Py_BuildValue("[ddd]",lo[0]-skin,lo[1]-skin,lo[2]-skin); if (!low) goto error;
    high=Py_BuildValue("[ddd]",hi[0]+skin,hi[1]+skin,hi[2]+skin); if (!high) goto error;
    if (observer!=Py_None) {
        call=PyTuple_Pack(3,entity,low,high); if (!call) goto error;
        value=PyObject_CallObject(observer,call); Py_CLEAR(call); if (!value) goto error;
        Py_CLEAR(value);
    }
    call=PyTuple_Pack(2,low,high); if (!call) goto error;
    near=PyObject_Call(near_fn,call,kw); Py_CLEAR(call); if (!near) goto error;
    vehicles=PySet_New(vehicle_values); if (!vehicles) goto error;
    if (PySet_Discard(vehicles,player)<0) goto error;
    bodies=PySet_New(NULL); if (!bodies) goto error;
    if (near!=Py_None) {
        iterator=PyObject_GetIter(near); if (!iterator) goto error;
        while ((item=PyIter_Next(iterator))) {
            int belongs;
            body=PySequence_GetItem(item,0); if (!body) goto error;
            belongs=PySequence_Contains(maps,body);
            if (belongs==0) belongs=PySet_Contains(vehicles,body);
            if (belongs<0 || (belongs && PySet_Add(bodies,body)<0)) goto error;
            Py_CLEAR(body); Py_CLEAR(item);
        }
        if (PyErr_Occurred()) goto error;
        Py_CLEAR(iterator);
    }
    points=PyList_New(0); if (!points) goto error;
    iterator=PyObject_GetIter(bodies); if (!iterator) goto error;
    while ((body=PyIter_Next(iterator))) {
        call=PyTuple_Pack(3,player,body,skin_object); if (!call) goto error;
        rows=PyObject_Call(closest_fn,call,kw); Py_CLEAR(call); if (!rows) goto error;
        row_iter=PyObject_GetIter(rows); if (!row_iter) goto error;
        while ((row=PyIter_Next(row_iter))) {
            if (PyList_Append(points,row)<0) goto error;
            Py_CLEAR(row);
        }
        if (PyErr_Occurred()) goto error;
        Py_CLEAR(row_iter); Py_CLEAR(rows); Py_CLEAR(body);
    }
    if (PyErr_Occurred()) goto error;
    goto cleanup;
error:
    Py_CLEAR(points);
cleanup:
    Py_XDECREF(joints_fn); Py_XDECREF(aabb_fn); Py_XDECREF(near_fn); Py_XDECREF(closest_fn);
    Py_XDECREF(kw); Py_XDECREF(call); Py_XDECREF(value); Py_XDECREF(bounds);
    Py_XDECREF(low); Py_XDECREF(high); Py_XDECREF(near); Py_XDECREF(vehicles); Py_XDECREF(bodies);
    Py_XDECREF(iterator); Py_XDECREF(item); Py_XDECREF(body); Py_XDECREF(rows);
    Py_XDECREF(row_iter); Py_XDECREF(row);
    return points;
}

static PyMethodDef methods[]={
    {"gather",gather,METH_VARARGS,"Collect contact candidates through the public PyBullet API."},
    {NULL,NULL,0,NULL}
};
static struct PyModuleDef module={PyModuleDef_HEAD_INIT,"_contact_queries",NULL,-1,methods,NULL,NULL,NULL,NULL};
PyMODINIT_FUNC PyInit__contact_queries(void) {
    PyObject *result=PyModule_Create(&module);
    if (result && PyModule_AddIntConstant(result,"API_VERSION",1)<0) {
        Py_DECREF(result);
        return NULL;
    }
    return result;
}
