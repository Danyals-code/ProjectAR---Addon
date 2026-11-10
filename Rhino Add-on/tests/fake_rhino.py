# SPDX-License-Identifier: GPL-3.0-or-later
"""A small stand-in for RhinoCommon, Eto and scriptcontext, so the Rhino
side of the add-on can run without Rhino.

It covers only what the add-on uses, with the names and behavior the
add-on expects (events fire in Rhino's order: a replace fires Replace,
Delete, then Add). It can't prove the real API matches; that needs Rhino.
It does run every line of the add-on's own logic.

install() puts the modules in sys.modules; import projectar after it.
"""

import ctypes
import itertools
import math
import struct
import sys
import types
import uuid

_serials = itertools.count(1)


# ---------------------------------------------------------------- basics


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def fire(self, sender=None, e=None):
        for handler in list(self.handlers):
            handler(sender, e)


class EnumValue:
    def __init__(self, type_name, name, value):
        self.type_name, self.name, self.value = type_name, name, value

    def __str__(self):
        return self.name

    def __repr__(self):
        return f"{self.type_name}.{self.name}"

    def __eq__(self, other):
        return isinstance(other, EnumValue) and other.type_name == self.type_name and other.name == self.name

    def __hash__(self):
        return hash((self.type_name, self.name))

    def __int__(self):
        return self.value


def enum(type_name, *names):
    return types.SimpleNamespace(**{n: EnumValue(type_name, n, i) for i, n in enumerate(names)})


class Guid:
    def __init__(self, text=None):
        self.text = str(text) if text is not None else str(uuid.uuid4())

    def __str__(self):
        return self.text

    def __eq__(self, other):
        return isinstance(other, Guid) and other.text == self.text

    def __hash__(self):
        return hash(self.text)


Guid.Empty = Guid("00000000-0000-0000-0000-000000000000")


class NetArray(list):
    def __init__(self, items, element="Single"):
        super().__init__(items)
        self._element = element

    @property
    def Length(self):
        return len(self)

    def GetType(self):
        element = self._element
        return types.SimpleNamespace(GetElementType=lambda: types.SimpleNamespace(Name=element))


class IntPtr:
    def __init__(self, value):
        self.value = int(value)


def marshal_copy(array, start, pointer, count):
    code = {"Single": "f", "Int32": "i", "Byte": "B", "Double": "d"}[array._element]
    data = struct.pack(f"<{count}{code}", *array[start:start + count])
    ctypes.memmove(pointer.value, data, len(data))


class _Pinned:
    def __init__(self, array, kind):
        code = {"Single": "f", "Int32": "i", "Byte": "B", "Double": "d"}[array._element]
        self.buffer = ctypes.create_string_buffer(struct.pack(f"<{len(array)}{code}", *array) or b"\0")

    def AddrOfPinnedObject(self):
        return types.SimpleNamespace(ToInt64=lambda: ctypes.addressof(self.buffer))

    def Free(self):
        pass


class NetList(list):
    def Add(self, item):
        self.append(item)

    @property
    def Count(self):
        return len(self)


class GenericList:
    def __getitem__(self, _type):
        return NetList


class Color:
    def __init__(self, a, r, g, b):
        self.A, self.R, self.G, self.B = a, r, g, b

    @staticmethod
    def FromArgb(a, r, g, b):
        return Color(a, r, g, b)


class Color4f:
    def __init__(self, r, g, b, a):
        self.R, self.G, self.B, self.A = float(r), float(g), float(b), float(a)


class Size:
    def __init__(self, width, height):
        self.Width, self.Height = width, height


# ---------------------------------------------------------------- geometry


class Point3d:
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = float(x), float(y), float(z)

    def __sub__(self, other):
        return Vector3d(self.X - other.X, self.Y - other.Y, self.Z - other.Z)

    def __add__(self, v):
        return Point3d(self.X + v.X, self.Y + v.Y, self.Z + v.Z)


class Vector3d(Point3d):
    @property
    def Length(self):
        return math.sqrt(self.X ** 2 + self.Y ** 2 + self.Z ** 2)


Point3d.Origin = Point3d(0, 0, 0)


class Transform:
    def __init__(self, diagonal=1.0):
        for r in range(4):
            for c in range(4):
                setattr(self, f"M{r}{c}", diagonal if r == c else 0.0)

    @staticmethod
    def Translation(x, y=None, z=None):
        if y is None:
            x, y, z = x.X, x.Y, x.Z
        return Transform.from_rows([[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z], [0, 0, 0, 1]])

    @staticmethod
    def from_rows(rows):
        xf = Transform(1.0)
        for r in range(4):
            for c in range(4):
                object.__setattr__(xf, f"M{r}{c}", float(rows[r][c]))
        return xf

    def __mul__(self, other):
        a, b = self.rows(), other.rows()
        return Transform.from_rows([[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)])

    @staticmethod
    def PlaneToPlane(source, target):
        assert source is Plane.WorldXY
        x, y, z, o = target.XAxis, target.YAxis, target.ZAxis, target.Origin
        return Transform.from_rows([[x.X, y.X, z.X, o.X], [x.Y, y.Y, z.Y, o.Y], [x.Z, y.Z, z.Z, o.Z], [0, 0, 0, 1]])

    @staticmethod
    def Scale(anchor, factor):
        return Transform.from_rows([[factor, 0, 0, 0], [0, factor, 0, 0], [0, 0, factor, 0], [0, 0, 0, 1]])

    def rows(self):
        return [[getattr(self, f"M{r}{c}") for c in range(4)] for r in range(4)]

    def apply(self, p, w=1.0):
        m = self.rows()
        return [sum(m[r][k] * v for k, v in enumerate((p[0], p[1], p[2], w))) for r in range(3)]


class Plane:
    def __init__(self, origin, x_axis, y_axis):
        def unit(v):
            n = math.sqrt(v.X ** 2 + v.Y ** 2 + v.Z ** 2)
            return Vector3d(v.X / n, v.Y / n, v.Z / n)

        self.Origin, self.XAxis = origin, unit(x_axis)
        y = unit(y_axis)
        self.ZAxis = unit(Vector3d(self.XAxis.Y * y.Z - self.XAxis.Z * y.Y, self.XAxis.Z * y.X - self.XAxis.X * y.Z,
                                   self.XAxis.X * y.Y - self.XAxis.Y * y.X))
        z = self.ZAxis
        self.YAxis = Vector3d(z.Y * self.XAxis.Z - z.Z * self.XAxis.Y, z.Z * self.XAxis.X - z.X * self.XAxis.Z,
                              z.X * self.XAxis.Y - z.Y * self.XAxis.X)


Plane.WorldXY = Plane(Point3d(0, 0, 0), Vector3d(1, 0, 0), Vector3d(0, 1, 0))


class _VertexList:
    def __init__(self, mesh, attr, width):
        self._mesh, self._attr, self._width = mesh, attr, width

    @property
    def Count(self):
        return len(getattr(self._mesh, self._attr))

    def ToFloatArray(self):
        return NetArray([v for item in getattr(self._mesh, self._attr) for v in item[:self._width]])

    def ComputeNormals(self):
        self._mesh.normals = [(0.0, 0.0, 1.0)] * len(self._mesh.vertices)
        return True


class _FaceList:
    def __init__(self, mesh):
        self._mesh = mesh

    @property
    def Count(self):
        return len(self._mesh.faces)

    @property
    def TriangleCount(self):
        return sum(1 for f in self._mesh.faces if len(f) == 3)

    @property
    def QuadCount(self):
        return sum(1 for f in self._mesh.faces if len(f) == 4)

    def ToIntArray(self, as_triangles):
        out = []
        for f in self._mesh.faces:
            out += f[:3]
            if len(f) == 4:
                out += [f[0], f[2], f[3]]
        return NetArray(out, "Int32")


class GeometryBase:
    def Duplicate(self):
        import copy

        return copy.deepcopy(self)

    def GetBoundingBox(self, accurate):
        points = self._points()
        return BoundingBox(Point3d(*[min(p[i] for p in points) for i in range(3)]),
                           Point3d(*[max(p[i] for p in points) for i in range(3)]))


class Mesh(GeometryBase):
    def __init__(self):
        self.vertices, self.normals, self.uvs, self.faces = [], [], [], []

    @property
    def Vertices(self):
        return _VertexList(self, "vertices", 3)

    @property
    def Normals(self):
        return _VertexList(self, "normals", 3)

    @property
    def TextureCoordinates(self):
        return _VertexList(self, "uvs", 2)

    @property
    def Faces(self):
        return _FaceList(self)

    def _points(self):
        return self.vertices

    def Append(self, other):
        base = len(self.vertices)
        self.vertices += other.vertices
        self.normals += other.normals
        self.uvs += other.uvs if len(other.uvs) == len(other.vertices) else []
        self.faces += [tuple(i + base for i in f) for f in other.faces]

    def DuplicateMesh(self):
        return self.Duplicate()

    def Transform(self, xf):
        self.vertices = [tuple(xf.apply(v)) for v in self.vertices]
        self.normals = [tuple(xf.apply(n, 0.0)) for n in self.normals]
        return True

    @staticmethod
    def CreateFromBrep(brep, params):
        return NetArray([brep.mesh.Duplicate()], "Mesh")

    @staticmethod
    def CreateFromClosedPolyline(polyline):
        mesh = Mesh()
        points = polyline.points[:-1]
        mesh.vertices = [(p.X, p.Y, p.Z) for p in points]
        mesh.normals = [(0.0, 0.0, 1.0)] * len(points)
        mesh.faces = [(0, i, i + 1) for i in range(1, len(points) - 1)]
        return mesh


def box_mesh(corners):
    mesh = Mesh()
    mesh.vertices = [tuple(c) for c in corners]
    mesh.normals = [(0.0, 0.0, 1.0)] * 8
    mesh.uvs = [(0.0, 0.0)] * 8
    mesh.faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return mesh


class Brep(GeometryBase):
    def __init__(self, mesh, faces=6):
        self.mesh = mesh
        self.Faces = types.SimpleNamespace(Count=faces)

    def _points(self):
        return self.mesh.vertices

    def Transform(self, xf):
        return self.mesh.Transform(xf)

    @staticmethod
    def CreateFromBox(corners):
        points = list(corners)
        assert len(points) == 8
        return Brep(box_mesh([(p.X, p.Y, p.Z) for p in points]))


class Extrusion(Brep):
    pass


class Surface(Brep):
    pass


class SubD(GeometryBase):
    pass


class TextDot(GeometryBase):
    def __init__(self, text, point):
        self.Text, self.Point = text, point

    def _points(self):
        return [(self.Point.X, self.Point.Y, self.Point.Z)]

    def Transform(self, xf):
        self.Point = Point3d(*xf.apply((self.Point.X, self.Point.Y, self.Point.Z)))


class Polyline:
    def __init__(self, points):
        self.points = list(points)


class Interval:
    def __init__(self, a, b):
        self.T0, self.T1 = a, b


class BoundingBox:
    def __init__(self, low=None, high=None):
        self.Min, self.Max = low, high

    @property
    def IsValid(self):
        return self.Min is not None

    @property
    def Center(self):
        return Point3d((self.Min.X + self.Max.X) / 2, (self.Min.Y + self.Max.Y) / 2, (self.Min.Z + self.Max.Z) / 2)

    @staticmethod
    def Union(a, b):
        if not a.IsValid:
            return b
        if not b.IsValid:
            return a
        return BoundingBox(Point3d(min(a.Min.X, b.Min.X), min(a.Min.Y, b.Min.Y), min(a.Min.Z, b.Min.Z)),
                           Point3d(max(a.Max.X, b.Max.X), max(a.Max.Y, b.Max.Y), max(a.Max.Z, b.Max.Z)))


BoundingBox.Empty = BoundingBox()


class Light(GeometryBase):
    def __init__(self, kind="point", location=(0, 0, 0), direction=(0, 0, -1), intensity=1.0):
        self.IsEnabled = True
        self.IsPointLight = kind == "point"
        self.IsSpotLight = kind == "spot"
        self.IsDirectionalLight = kind == "sun"
        self.IsRectangularLight = kind == "area"
        self.IsLinearLight = False
        self.Intensity = intensity
        self.Location = Point3d(*location)
        self.Direction = Vector3d(*direction)
        self.Length = Vector3d(1, 0, 0)
        self.Width = Vector3d(0, 1, 0)
        self.Diffuse = Color(255, 255, 240, 200)
        self.ShadowIntensity = 1.0
        self.PowerWatts = 0.0
        self.SpotAngleRadians = math.radians(30)
        self.HotSpot = 0.5


# ---------------------------------------------------------------- document objects

ObjectType = enum("ObjectType", "Brep", "Extrusion", "Mesh", "SubD", "Surface", "InstanceReference", "TextDot", "Light", "Curve")
ObjectMaterialSource = enum("ObjectMaterialSource", "MaterialFromLayer", "MaterialFromObject", "MaterialFromParent")
ObjectColorSource = enum("ObjectColorSource", "ColorFromLayer", "ColorFromObject", "ColorFromMaterial", "ColorFromParent")
ActiveSpace = enum("ActiveSpace", "None_", "ModelSpace", "PageSpace")
TextureType = enum("TextureType", "Bitmap", "Bump", "Transparency", "Environment", "PBR_BaseColor", "PBR_Metallic",
                   "PBR_Roughness", "PBR_Alpha")
MeshType = enum("MeshType", "Default", "Render", "Analysis", "Preview", "Any")
UnitSystem = enum("UnitSystem", "Millimeters", "Centimeters", "Meters", "Inches")
Result = enum("Result", "Success", "Cancel", "Nothing", "Failure")
_UNITS = {"Millimeters": 0.001, "Centimeters": 0.01, "Meters": 1.0, "Inches": 0.0254}


class ObjectAttributes:
    def __init__(self):
        self.LayerIndex = 0
        self.Visible = True
        self.Name = ""
        self.MaterialSource = ObjectMaterialSource.MaterialFromLayer
        self.MaterialIndex = -1
        self.ColorSource = ObjectColorSource.ColorFromLayer
        self.ObjectColor = Color(255, 0, 0, 0)
        self.Space = ActiveSpace.ModelSpace
        self._strings = {}
        self._render_material = None

    def GetUserString(self, key):
        return self._strings.get(key)

    def SetUserString(self, key, value):
        if value is None:
            self._strings.pop(key, None)
        else:
            self._strings[key] = str(value)
        return True

    def Duplicate(self):
        import copy

        twin = copy.copy(self)
        twin._strings = dict(self._strings)
        return twin

    def _no_getter(self):
        raise TypeError("property cannot be read")  # as pythonnet says for a set-only property

    def _set_render_material(self, material):
        self._render_material = material
        self.MaterialSource = ObjectMaterialSource.MaterialFromObject
        self.MaterialIndex = material.material_index

    RenderMaterial = property(_no_getter, _set_render_material)


class RhinoObject:
    def __init__(self, doc, geometry, attributes, object_type, oid=None):
        self.Document = doc
        self.Id = oid or Guid()
        self.Geometry = geometry
        self.Attributes = attributes
        self.ObjectType = object_type
        self.RuntimeSerialNumber = next(_serials)
        self.IsDeleted = False
        self.IsHidden = False
        self.selected = False
        self.meshes_made = False

    @property
    def Visible(self):
        return self.Attributes.Visible and not self.IsHidden

    def IsSelected(self, check_subobjects):
        return 1 if self.selected else 0

    def GetMeshes(self, mesh_type):
        geometry = self.Geometry
        if isinstance(geometry, Brep) and self.meshes_made:
            return NetArray([geometry.mesh.Duplicate()], "Mesh")
        return NetArray([], "Mesh")

    def CreateMeshes(self, mesh_type, params, ignore_custom):
        self.meshes_made = True
        return 1


class InstanceReferenceGeometry(GeometryBase):
    def GetBoundingBox(self, accurate):
        return BoundingBox.Empty


class InstanceObject(RhinoObject):
    def __init__(self, doc, idef, xform, attributes, oid=None):
        super().__init__(doc, InstanceReferenceGeometry(), attributes, ObjectType.InstanceReference, oid)
        self.InstanceDefinition = idef
        self.InstanceXform = xform


class LightObject(RhinoObject):
    def __init__(self, doc, light, attributes):
        super().__init__(doc, light, attributes, ObjectType.Light)
        self.LightGeometry = light


class Layer:
    def __init__(self):
        self.Name = ""
        self.Id = Guid()
        self.ParentLayerId = Guid.Empty
        self.IsVisible = True
        self.RenderMaterialIndex = -1
        self.Color = Color(255, 200, 200, 200)
        self.Index = -1
        self.SortIndex = -1
        self.IsDeleted = False
        self.table = None
        self.persistent = True

    def GetPersistentVisibility(self):
        return self.persistent

    def SetPersistentVisibility(self, value):
        self.persistent = bool(value)

    @property
    def FullPath(self):
        parent = self.table.by_id(self.ParentLayerId) if self.table else None
        return f"{parent.FullPath}::{self.Name}" if parent else self.Name



class PhysicallyBased:
    def __init__(self):
        self.BaseColor = Color4f(0.8, 0.8, 0.8, 1.0)
        self.Metallic = 0.0
        self.Roughness = 0.5
        self.Alpha = 1.0
        self.Opacity = 1.0


class Texture:
    def __init__(self, path):
        self.FileName = path
        self.Enabled = True
        self.FileReference = types.SimpleNamespace(FullPath=path, RelativePath="")


class Material:
    MaxShine = 255.0

    def __init__(self):
        self.Name = ""
        self.DiffuseColor = Color(255, 200, 200, 200)
        self.Transparency = 0.0
        self.Shine = 0.0
        self.IsDeleted = False
        self.Index = -1
        self.IsPhysicallyBased = False
        self.PhysicallyBased = None
        self.textures = {}
        self._strings = {}

    def ToPhysicallyBased(self):
        self.IsPhysicallyBased = True
        self.PhysicallyBased = PhysicallyBased()

    def GetTexture(self, which):
        return self.textures.get(which.name)

    def GetUserString(self, key):
        return self._strings.get(key)

    def SetUserString(self, key, value):
        self._strings[key] = value


class RenderMaterial:
    def __init__(self, material, index):
        self.Name = material.Name
        self.Id = Guid()
        self.material = material
        self.material_index = index

    def ToMaterial(self, generation):
        return self.material


# ---------------------------------------------------------------- tables


class ObjectTable:
    def __init__(self, doc):
        self.doc = doc
        self.objects = []

    def _fire(self, name, e):
        getattr(RhinoDoc, name).fire(self.doc, e)

    def add(self, obj):
        self.objects.append(obj)
        self._fire("AddRhinoObject", types.SimpleNamespace(ObjectId=obj.Id, TheObject=obj))
        return obj.Id

    def add_geometry(self, geometry, attributes=None, object_type=None):
        attributes = attributes or ObjectAttributes()
        object_type = object_type or {Brep: ObjectType.Brep, Mesh: ObjectType.Mesh, TextDot: ObjectType.TextDot}[type(geometry)]
        return self.FindId(self.add(RhinoObject(self.doc, geometry, attributes, object_type)))

    def AddBrep(self, brep, attributes):
        return self.add(RhinoObject(self.doc, brep, attributes, ObjectType.Brep))

    def AddMesh(self, mesh, attributes):
        return self.add(RhinoObject(self.doc, mesh, attributes, ObjectType.Mesh))

    def AddTextDot(self, dot, attributes):
        return self.add(RhinoObject(self.doc, dot, attributes, ObjectType.TextDot))

    def FindId(self, oid):
        return next((o for o in self.objects if o.Id == oid and not o.IsDeleted), None)

    def GetObjectList(self, settings):
        return [o for o in self.objects if not o.IsDeleted and o.ObjectType != ObjectType.Light]

    def GetSelectedObjects(self, include_lights, include_grips):
        return [o for o in self.objects if o.selected and not o.IsDeleted]

    def replace(self, obj, geometry):
        """Rhino replaces an edited object with a new one under the same id."""
        new = type(obj).__new__(type(obj))
        new.__dict__.update(obj.__dict__)
        new.Geometry = geometry
        new.RuntimeSerialNumber = next(_serials)
        new.meshes_made = False
        self._fire("ReplaceRhinoObject", types.SimpleNamespace(ObjectId=obj.Id, OldRhinoObject=obj, NewRhinoObject=new))
        self.objects[self.objects.index(obj)] = new
        obj.IsDeleted = True
        self._fire("DeleteRhinoObject", types.SimpleNamespace(ObjectId=obj.Id, TheObject=obj))
        self._fire("AddRhinoObject", types.SimpleNamespace(ObjectId=new.Id, TheObject=new))
        return new

    def move(self, obj, xf):
        """What the Move command does: BeforeTransformObjects, then a replace."""
        self._fire("BeforeTransformObjects", types.SimpleNamespace(
            Transform=xf, ObjectsWillBeCopied=False, GripCount=0, Objects=[obj]))
        if isinstance(obj, InstanceObject):
            new = self.replace(obj, obj.Geometry)
            rows = [[sum(a * b for a, b in zip(row, col)) for col in zip(*obj.InstanceXform.rows())] for row in xf.rows()]
            moved = Transform(1.0)
            for r in range(4):
                for c in range(4):
                    setattr(moved, f"M{r}{c}", rows[r][c])
            new.InstanceXform = moved
            return new
        geometry = obj.Geometry.Duplicate()
        geometry.Transform(xf)
        return self.replace(obj, geometry)

    def Delete(self, oid, quiet):
        obj = self.FindId(oid)
        if obj is None:
            return False
        obj.IsDeleted = True
        self._fire("DeleteRhinoObject", types.SimpleNamespace(ObjectId=oid, TheObject=obj))
        return True

    def ModifyAttributes(self, obj, attributes, quiet):
        old = obj.Attributes
        obj.Attributes = attributes
        self._fire("ModifyObjectAttributes", types.SimpleNamespace(RhinoObject=obj, OldAttributes=old, NewAttributes=attributes))
        return True

    def Select(self, oid, select):
        obj = self.FindId(oid)
        if obj is not None:
            obj.selected = select
            RhinoDoc.SelectObjects.fire(self.doc, types.SimpleNamespace(RhinoObjects=[obj]))
        return True

    def UnselectAll(self):
        for obj in self.objects:
            obj.selected = False
        RhinoDoc.DeselectAllObjects.fire(self.doc, None)
        return len(self.objects)


class LayerTable:
    def __init__(self, doc):
        self.doc = doc
        self.layers = []
        self.CurrentLayerIndex = 0

    def __iter__(self):
        return iter(list(self.layers))

    def by_id(self, layer_id):
        return next((l for l in self.layers if l.Id == layer_id), None)

    def Add(self, layer):
        siblings = [l for l in self.layers if l.ParentLayerId == layer.ParentLayerId and not l.IsDeleted]
        if any(l.Name == layer.Name for l in siblings):
            return -1
        layer.Index = len(self.layers)
        layer.SortIndex = layer.Index
        layer.table = self
        parent = self.by_id(layer.ParentLayerId)
        if parent is not None and not parent.IsVisible:
            layer.persistent, layer.IsVisible = layer.IsVisible, False
        self.layers.append(layer)
        RhinoDoc.LayerTableEvent.fire(self.doc, None)
        return layer.Index

    def FindIndex(self, index):
        return self.layers[index] if 0 <= index < len(self.layers) else None

    def Modify(self, layer, index, quiet):
        if index == self.CurrentLayerIndex and not layer.IsVisible:
            layer.IsVisible = True  # Rhino keeps the current layer on
            return False
        self.layers[index] = layer
        self._cascade(layer)
        RhinoDoc.LayerTableEvent.fire(self.doc, None)
        return True

    def _cascade(self, parent):
        """Rhino turns sublayers off with their parent and back on as their
        persistent visibility says."""
        for child in self.layers:
            if child.ParentLayerId == parent.Id:
                child.IsVisible = parent.IsVisible and child.persistent
                self._cascade(child)

    def SetCurrentLayerIndex(self, index, quiet):
        self.CurrentLayerIndex = index
        return True


class MaterialTable:
    def __init__(self, doc):
        self.doc = doc
        self.materials = []

    @property
    def Count(self):
        return len(self.materials)

    def __getitem__(self, index):
        return self.materials[index]

    def Add(self, material):
        self.materials.append(material)
        index = len(self.materials) - 1
        material.Index = index
        self.doc.RenderMaterials.append(RenderMaterial(material, index))
        RhinoDoc.MaterialTableEvent.fire(self.doc, None)
        return index

    def Modify(self, material, index, quiet):
        material.Index = index
        self.materials[index] = material
        RhinoDoc.MaterialTableEvent.fire(self.doc, None)
        return True


class InstanceDefinition:
    def __init__(self, doc, name, objects, index):
        self.doc, self.Name, self.objects, self.Index = doc, name, objects, index
        self.Id = Guid()
        self.IsDeleted = False
        self.IsReference = False

    def GetObjects(self):
        return list(self.objects)


class InstanceDefinitionTable:
    def __init__(self, doc):
        self.doc = doc
        self.items = []

    def __getitem__(self, index):
        return self.items[index]

    def __iter__(self):
        return iter(list(self.items))

    def add(self, name, objects):
        idef = InstanceDefinition(self.doc, name, objects, len(self.items))
        self.items.append(idef)
        return idef

    def ModifyGeometry(self, index, geometry, attributes):
        idef = self.items[index]
        idef.objects = [RhinoObject(self.doc, g, a, old.ObjectType) for g, a, old in zip(geometry, attributes, idef.objects)]
        RhinoDoc.InstanceDefinitionTableEvent.fire(self.doc, None)
        return True


class LightTable:
    def __init__(self, doc):
        self.doc = doc
        self.lights = []
        self.Sun = types.SimpleNamespace(Enabled=False, Light=None)

    @property
    def Count(self):
        return len(self.lights)

    def __getitem__(self, index):
        return self.lights[index]


class RhinoViewport:
    def __init__(self, name):
        self.Name = name
        self.CameraLocation = Point3d(1000, -1000, 800)
        self.CameraTarget = Point3d(0, 0, 0)
        self.CameraUp = Vector3d(0, 0, 1)
        self.Camera35mmLensLength = 50.0
        self.IsParallelProjection = False
        self.IsPerspectiveProjection = True
        self.Size = Size(1200, 800)

    @property
    def CameraDirection(self):
        return self.CameraTarget - self.CameraLocation

    def SetCameraLocations(self, target, camera):
        self.CameraTarget, self.CameraLocation = target, camera
        return True

    def ChangeToPerspectiveProjection(self, symmetric, lens):
        self.IsParallelProjection, self.IsPerspectiveProjection = False, True
        return True


class ViewportInfo:
    def __init__(self, vp):
        self.CameraLocation = vp.CameraLocation
        self.CameraDirection = vp.CameraDirection
        self.CameraUp = vp.CameraUp
        self.IsPerspectiveProjection = vp.IsPerspectiveProjection
        self.FrustumNear, self.FrustumFar = 1.0, 10000.0
        half = math.tan(math.radians(20))
        aspect = vp.Size.Width / vp.Size.Height
        self.FrustumTop, self.FrustumBottom = half, -half
        self.FrustumRight, self.FrustumLeft = half * aspect, -half * aspect
        self.FrustumAspect = aspect


class RhinoView:
    def __init__(self, name):
        self.ActiveViewport = RhinoViewport(name)
        self.redraws = 0

    def Redraw(self):
        self.redraws += 1

    def CaptureToBitmap(self, size):
        def save(path, fmt):
            with open(path, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0fakejpeg")

        return types.SimpleNamespace(Save=save)


class RhinoPageView(RhinoView):
    pass


class ViewTable:
    def __init__(self):
        self.ActiveView = RhinoView("Perspective")

    def Redraw(self):
        pass

    def GetViewList(self, view_filter):
        assert view_filter == "model"
        return [self.ActiveView]


class NamedViewTable:
    def __init__(self):
        self.views = []

    @property
    def Count(self):
        return len(self.views)

    def __getitem__(self, index):
        return self.views[index]

    def add(self, name, viewport):
        self.views.append(types.SimpleNamespace(Name=name, Viewport=ViewportInfo(viewport)))


class StringTable:
    def __init__(self):
        self.values = {}

    def GetValue(self, section, entry):
        return self.values.get((section, entry))

    def SetString(self, section, entry, value):
        self.values[(section, entry)] = value
        return ""

    def Delete(self, section, entry):
        self.values.pop((section, entry), None)


class RhinoDoc:
    ActiveDoc = None

    def __init__(self, name="Model.3dm"):
        self.RuntimeSerialNumber = next(_serials)
        self.Name = name
        self.Path = ""
        self.ModelUnitSystem = UnitSystem.Millimeters
        self.MeshingParameterStyle = 0
        self.RenderMaterials = []
        self.Objects = ObjectTable(self)
        self.Layers = LayerTable(self)
        self.Materials = MaterialTable(self)
        self.InstanceDefinitions = InstanceDefinitionTable(self)
        self.Lights = LightTable(self)
        self.Views = ViewTable()
        self.NamedViews = NamedViewTable()
        self.Strings = StringTable()
        self.undo = []
        default = Layer()
        default.Name = "Default"
        self.Layers.Add(default)

    def GetMeshingParameters(self, style):
        return object()

    def BeginUndoRecord(self, name):
        self.undo.append(name)
        return len(self.undo)

    def EndUndoRecord(self, record):
        return True

    def layer(self, name, parent=None, visible=True, material=-1):
        layer = Layer()
        layer.Name = name
        layer.IsVisible = visible
        layer.RenderMaterialIndex = material
        if parent is not None:
            layer.ParentLayerId = self.Layers.layers[parent].Id
        return self.Layers.Add(layer)


for _name in ("AddRhinoObject", "DeleteRhinoObject", "UndeleteRhinoObject", "ReplaceRhinoObject",
              "ModifyObjectAttributes", "BeforeTransformObjects", "SelectObjects", "DeselectObjects",
              "DeselectAllObjects", "LayerTableEvent", "MaterialTableEvent", "RenderMaterialsTableEvent",
              "InstanceDefinitionTableEvent", "LightTableEvent", "DocumentPropertiesChanged",
              "EndOpenDocument", "NewDocument", "ActiveDocumentChanged", "EndSaveDocument"):
    setattr(RhinoDoc, _name, Event())


class ObjectEnumeratorSettings:
    pass


class GetPoint:
    picked = Point3d(10, 20, 0)

    def SetCommandPrompt(self, prompt):
        pass

    def Get(self):
        return None

    def CommandResult(self):
        return Result.Success

    def Point(self):
        return GetPoint.picked


# ---------------------------------------------------------------- Eto


class Widget:
    EVENTS = ()

    def __init__(self, *args):
        self.Visible = True
        self.Enabled = True
        self.ToolTip = None
        self.Font = None
        self.TextColor = None
        self.Size = None
        for name in self.EVENTS:
            setattr(self, name, Event())


class _Text(Widget):
    EVENTS = ("TextChanged",)

    def __init__(self, *args):
        self._text = ""
        super().__init__()
        self.PlaceholderText = ""
        self.Wrap = None
        self.ReadOnly = False

    @property
    def Text(self):
        return self._text

    @Text.setter
    def Text(self, value):
        changed = value != self._text
        self._text = value
        if changed and hasattr(self, "TextChanged"):
            self.TextChanged.fire(self, None)


class Label(_Text):
    pass


class Button(_Text):
    EVENTS = ("TextChanged", "Click")

    def click(self):
        self.Click.fire(self, None)


class TextBox(_Text):
    pass


class TextArea(_Text):
    pass


class CheckBox(_Text):
    EVENTS = ("TextChanged", "CheckedChanged")

    def __init__(self, *args):
        self._checked = False
        super().__init__()

    @property
    def Checked(self):
        return self._checked

    @Checked.setter
    def Checked(self, value):
        changed = bool(value) != self._checked
        self._checked = bool(value)
        if changed:
            self.CheckedChanged.fire(self, None)


class _Items(list):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def Add(self, item):
        self.append(item)

    def Clear(self):
        self.clear()
        self.owner.SelectedIndex = -1


class DropDown(Widget):
    EVENTS = ("SelectedIndexChanged",)

    def __init__(self, *args):
        self._index = -1
        super().__init__()
        self.Items = _Items(self)

    @property
    def SelectedIndex(self):
        return self._index

    @SelectedIndex.setter
    def SelectedIndex(self, value):
        assert -1 <= value < len(self.Items), f"index {value} of {len(self.Items)} items"
        changed = value != self._index
        self._index = value
        if changed:
            self.SelectedIndexChanged.fire(self, None)


class NumericStepper(Widget):
    EVENTS = ("ValueChanged",)

    def __init__(self, *args):
        self._value = 0.0
        super().__init__()
        self.MinValue, self.MaxValue, self.DecimalPlaces, self.Increment = 0.0, 100.0, 0, 1.0

    @property
    def Value(self):
        return self._value

    @Value.setter
    def Value(self, value):
        value = min(max(float(value), self.MinValue), self.MaxValue)
        changed = value != self._value
        self._value = value
        if changed:
            self.ValueChanged.fire(self, None)


class Container(Widget):
    def __init__(self, *args):
        super().__init__()
        self.Content = None
        self.Padding = None
        self.Text = ""
        self.Title = ""


class Panel(Container):
    pass


class GroupBox(Container):
    pass


class Scrollable(Container):
    pass


class TabPage(Container):
    pass


class StackLayoutItem:
    def __init__(self, control, expand=False):
        assert control is not None
        self.Control, self.Expand = control, expand


class StackLayout(Container):
    def __init__(self, *args):
        super().__init__()
        self.Items = _ItemsList()


class _ItemsList(list):
    def Add(self, item):
        assert isinstance(item, StackLayoutItem)
        self.append(item)


class _Pages(list):
    def Add(self, item):
        self.append(item)


class TabControl(Widget):
    def __init__(self, *args):
        super().__init__()
        self.Pages = _Pages()
        self.SelectedPage = None


class Form(Container):
    EVENTS = ("Closed",)

    def __init__(self, *args):
        super().__init__()
        self.shown = False

    def Show(self):
        self.shown = True

    def BringToFront(self):
        pass

    def Close(self):
        self.shown = False
        self.Closed.fire(self, None)


class Dialog(Form):
    on_show = None  # a test sets this to act on the dialog while it's "open"

    def ShowModal(self, owner=None):
        if Dialog.on_show is not None:
            Dialog.on_show(self)


class UITimer:
    def __init__(self):
        self.Interval = 1.0
        self.Elapsed = Event()
        self.running = False

    def Start(self):
        self.running = True

    def Stop(self):
        self.running = False


class Clipboard:
    text = None

    @property
    def Text(self):
        return Clipboard.text

    @Text.setter
    def Text(self, value):
        Clipboard.text = value


class _FileDialog:
    result = "Ok"
    path = ""

    def __init__(self):
        self.Filters = NetList()
        self.FileName = ""
        self.Directory = ""
        self.Title = ""

    def ShowDialog(self, owner):
        if self.FileName:
            self.FileName = _FileDialog.path or self.FileName
        self.Directory = _FileDialog.path
        return getattr(DialogResult, _FileDialog.result)


class SaveFileDialog(_FileDialog):
    pass


class SelectFolderDialog(_FileDialog):
    pass


class FileFilter:
    def __init__(self, name, *extensions):
        self.Name, self.Extensions = name, extensions


class ImageView(Widget):
    def __init__(self):
        super().__init__()
        self.Image = None


DialogResult = enum("DialogResult", "None_", "Ok", "Cancel", "Yes", "No")


class MessageBox:
    answer = "Yes"

    @staticmethod
    def Show(parent, text, caption, buttons, kind):
        return getattr(DialogResult, MessageBox.answer)


class Bitmap:
    def __init__(self, source, width=None, height=None, interpolation=None):
        self.Width, self.Height = (width, height) if width else (64, 48)

    def ToByteArray(self, fmt):
        return NetArray(list(b"\x89PNG\r\n\x1a\nscaled"), "Byte")

    def Save(self, path, fmt):
        with open(path, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0eto")


# ---------------------------------------------------------------- install


def _module(name, **members):
    module = types.ModuleType(name)
    module.__dict__.update(members)
    sys.modules[name] = module
    return module


def install():
    drawing_ns = _module(
        "System.Drawing", Color=Color, Size=Size,
        Imaging=types.SimpleNamespace(ImageFormat=types.SimpleNamespace(Jpeg="jpeg", Png="png")),
    )
    generic = _module("System.Collections.Generic", List=GenericList())
    interop = types.SimpleNamespace(
        Marshal=types.SimpleNamespace(Copy=marshal_copy),
        GCHandle=types.SimpleNamespace(Alloc=_Pinned),
        GCHandleType=types.SimpleNamespace(Pinned="pinned"),
    )
    _module(
        "System", Guid=Guid, IntPtr=IntPtr, Int64=int, Drawing=drawing_ns,
        Collections=types.SimpleNamespace(Generic=generic),
        Runtime=types.SimpleNamespace(InteropServices=interop),
    )
    geometry = _module(
        "Rhino.Geometry", Mesh=Mesh, Brep=Brep, Extrusion=Extrusion, Surface=Surface, SubD=SubD, TextDot=TextDot,
        Point3d=Point3d, Vector3d=Vector3d, Transform=Transform, Plane=Plane, Polyline=Polyline, Interval=Interval,
        BoundingBox=BoundingBox, MeshType=MeshType, GeometryBase=GeometryBase, Light=Light,
        MeshingParameters=types.SimpleNamespace(Default=object()),
    )
    docobjects = _module(
        "Rhino.DocObjects", ObjectType=ObjectType, ObjectAttributes=ObjectAttributes, ObjectMaterialSource=ObjectMaterialSource,
        ObjectColorSource=ObjectColorSource, ActiveSpace=ActiveSpace, TextureType=TextureType, Material=Material,
        Layer=Layer, ObjectEnumeratorSettings=ObjectEnumeratorSettings, ViewportInfo=ViewportInfo,
    )
    render = types.SimpleNamespace(
        RenderContent=types.SimpleNamespace(ContentChanged=Event()),
        RenderTexture=types.SimpleNamespace(TextureGeneration=types.SimpleNamespace(Allow="allow")),
    )
    commands = types.SimpleNamespace(
        Command=types.SimpleNamespace(BeginCommand=Event(), EndCommand=Event(), UndoRedo=Event()), Result=Result)
    rhino = _module(
        "Rhino", RhinoDoc=RhinoDoc, Geometry=geometry, DocObjects=docobjects, Render=render, Commands=commands,
        UnitSystem=UnitSystem,
        RhinoMath=types.SimpleNamespace(UnitScale=lambda a, b: _UNITS[a.name] / _UNITS[b.name]),
        RhinoApp=types.SimpleNamespace(WriteLine=lambda text: None, Version="8.13 (fake)", Closing=Event()),
        Display=types.SimpleNamespace(Color4f=Color4f, RhinoPageView=RhinoPageView,
                                      ViewTypeFilter=types.SimpleNamespace(Model="model")),
        UI=types.SimpleNamespace(RhinoEtoApp=types.SimpleNamespace(MainWindow=None, MainWindowForDocument=lambda doc: "window"),
                                 EtoExtensions=types.SimpleNamespace(ToEto=lambda bitmap: Bitmap(None))),
        Input=types.SimpleNamespace(Custom=types.SimpleNamespace(GetPoint=GetPoint)),
    )
    forms = _module(
        "Eto.Forms", Label=Label, Button=Button, TextBox=TextBox, TextArea=TextArea, CheckBox=CheckBox,
        DropDown=DropDown, NumericStepper=NumericStepper, Panel=Panel, GroupBox=GroupBox, Scrollable=Scrollable,
        TabPage=TabPage, TabControl=TabControl, StackLayout=StackLayout, StackLayoutItem=StackLayoutItem, Form=Form,
        Dialog=Dialog, UITimer=UITimer, Clipboard=Clipboard, SaveFileDialog=SaveFileDialog,
        SelectFolderDialog=SelectFolderDialog, FileFilter=FileFilter, ImageView=ImageView, MessageBox=MessageBox,
        DialogResult=DialogResult, MessageBoxButtons=enum("MessageBoxButtons", "OK", "YesNo"),
        MessageBoxType=enum("MessageBoxType", "Information", "Question"),
        Orientation=enum("Orientation", "Horizontal", "Vertical"),
        HorizontalAlignment=enum("HorizontalAlignment", "Left", "Center", "Right", "Stretch"),
        VerticalAlignment=enum("VerticalAlignment", "Top", "Center", "Bottom", "Stretch"),
        WrapMode=enum("WrapMode", "None_", "Word", "Character"),
        BorderType=types.SimpleNamespace(**{"None": "none", "Line": "line"}),
    )
    drawing = _module(
        "Eto.Drawing", Size=Size, Padding=lambda *a: a, Bitmap=Bitmap,
        Colors=types.SimpleNamespace(Red="red", OrangeRed="orangered"),
        SystemFonts=types.SimpleNamespace(Bold=lambda *a: "bold"),
        ImageFormat=types.SimpleNamespace(Png="png", Jpeg="jpeg"),
        ImageInterpolation=types.SimpleNamespace(High="high"),
    )
    _module("Eto", Forms=forms, Drawing=drawing)
    _module("scriptcontext", sticky={})
    sys.modules["Rhino"] = rhino
    return rhino
