"""Create one textured area light from a detected light source.

Runs as a Python SOP inside the last for-each loop of the HDA (one iteration
per detected light source). For every light it

  1. converts the grid's rotation matrix into Euler angles,
  2. measures the grid to get the area light size,
  3. renders the cropped HDRI region as the light's emission texture,
  4. creates the area light on /obj and links the texture.

On the first iteration it also starts a new texture version folder and
renders the painted-out HDRI for the dome light.

Inputs:
  0  grid geometry of this light (4 points, placed and sized in the loop before)
  1  a point carrying the grid's 3x3 `transform` matrix (from Extract Transform)
  2  loop metadata with the `iteration` detail attribute

Node paths below are relative to this node inside the HDA.
"""

import math
import os
import re

import hou

TEXTURE_ROOT = os.path.join(hou.getenv("HIP"), "light_textures")
AREA_LIGHT = 2  # light_type menu index for area lights


# --- texture versioning ------------------------------------------------------

def texture_version_folder(first_iteration):
    """Return the folder for this run's textures.

    The first iteration creates the next free version folder (v001, v002, ...),
    all later iterations write into that same folder.
    """
    os.makedirs(TEXTURE_ROOT, exist_ok=True)
    versions = [
        int(match.group(1))
        for name in os.listdir(TEXTURE_ROOT)
        if os.path.isdir(os.path.join(TEXTURE_ROOT, name))
        and (match := re.match(r"v(\d+)", name))
    ]
    current = max(versions, default=0)

    if first_iteration:
        folder = os.path.join(TEXTURE_ROOT, f"v{current + 1:03d}")
        os.makedirs(folder)
        return folder
    return os.path.join(TEXTURE_ROOT, f"v{current:03d}")


# --- geometry -----------------------------------------------------------------

def matrix_to_euler_degrees(m):
    """Decompose a 3x3 rotation matrix (9 floats, row-major) into (x, y, z) degrees.

    Standard roll/pitch/yaw decomposition, including the gimbal-lock case
    where pitch is +-90 degrees.
    """
    r00, r01, r02, r10, r11, r12, r20, r21, r22 = m

    if abs(r20) != 1:
        pitch = -math.asin(r20)
        cos_pitch = math.cos(pitch)
        roll = math.atan2(r21 / cos_pitch, r22 / cos_pitch)
        yaw = math.atan2(r10 / cos_pitch, r00 / cos_pitch)
    else:  # gimbal lock
        yaw = 0
        if r20 == -1:
            pitch = math.pi / 2
            roll = yaw + math.atan2(r01, r02)
        else:
            pitch = -math.pi / 2
            roll = -yaw + math.atan2(-r01, -r02)

    return [math.degrees(a) for a in (roll, pitch, yaw)]


def grid_center_and_size(geo):
    """Centre and edge lengths of a 2x2 grid.

    Houdini numbers the points of a 2x2 grid row by row, so point 1 is the
    neighbour of point 0 along the first edge and point 2 along the second.
    """
    p0, p1, p2 = (geo.points()[i].position() for i in range(3))
    center = geo.boundingBox().center()
    return center, (p1 - p0).length(), (p2 - p0).length()


# --- rendering -----------------------------------------------------------------

def render_light_texture(texture_path, rot_x):
    """Render the HDRI crop for this light through the COP network.

    Lights rotated by more than 90 degrees around X face the other way,
    so their texture is flipped before rendering.
    """
    rop = hou.node("../texture_export/light_texture_export")
    flip = hou.node("../texture_export/flip1")

    flip.bypass(abs(rot_x) <= 90)
    hou.node("../OUT_TO_COPS").cook(force=True)    # current grid's UV bounds
    hou.node("../texture_export/crop1").cook(force=True)
    rop.parm("copoutput").set(texture_path)
    rop.render()
    flip.bypass(True)


def render_environment(env_path):
    """Render the painted-out HDRI used by the dome light."""
    rop_env = hou.node("../../hdri_process/render_env")
    rop_env.parm("copoutput").set(env_path)
    rop_env.render()


# --- light creation -----------------------------------------------------------

def create_area_light(name, center, rotation, size_x, size_y, texture_path):
    light = hou.node("/obj").createNode("custom_light", name)
    light.parm("light_type").set(AREA_LIGHT)
    light.parmTuple("position_vector").set(center)
    light.parm("distance_mult").set(1)
    light.parmTuple("rotate").set(rotation)
    light.parm("area_sizex").set(size_x)
    light.parm("area_sizey").set(size_y)
    light.parm("light_intensity").set(1.0)
    light.parm("light_enable").set(True)
    light.parm("light_texture").set(texture_path)
    return light


def create_dome_light(env_path):
    dome = hou.node("/obj").createNode("envlight", "Dome")
    dome.parm("env_map").set(env_path)
    return dome


# --- main ---------------------------------------------------------------------

def main():
    node = hou.pwd()
    grid_geo = node.inputs()[0].geometry()
    xform_geo = node.inputs()[1].geometry()
    loop_info = node.inputs()[2].geometry()

    iteration = loop_info.attribValue("iteration")
    first = iteration == 0

    folder = texture_version_folder(first)
    light_name = f"light_{iteration}"
    texture_path = f"{folder}/lightTexture.{light_name}.exr"

    matrix = xform_geo.iterPoints()[0].attribValue("transform")
    rotation = matrix_to_euler_degrees(matrix)
    center, size_x, size_y = grid_center_and_size(grid_geo)

    render_light_texture(texture_path, rotation[0])
    create_area_light(light_name, center, rotation, size_x, size_y, texture_path)

    if first:
        env_path = f"{folder}/Environment.exr"
        render_environment(env_path)
        create_dome_light(env_path)

    hou.node("/obj").layoutChildren()


main()
