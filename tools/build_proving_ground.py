"""Bake the project's own terrain, map sprites and meshes (no client inputs)."""
import argparse
import base64
import gzip
import io
import json
from pathlib import Path
import struct
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from solbreaker import proving_ground_layout as layout
from solbreaker.proving_ground_horizon import horizon_meshes
from solbreaker.proving_ground_signs import SIGNS, sign_meshes
from solbreaker.proving_ground_map import BUNDLE, KEY, PARENT


def sprite(image, size, *, named=True):
    image = ImageOps.fit(image.convert('RGB'), size, method=Image.Resampling.LANCZOS)
    # Each packed DAVA sprite has a power-of-two texture and an unpadded frame.
    width,height = size
    surface = Image.new('RGBA',(1 << (width-1).bit_length(), 1 << (height-1).bit_length()))
    surface.paste(image,(0,0))
    stream = io.BytesIO()
    surface.save(stream,'WEBP',quality=90,method=5)
    raw = stream.getvalue()
    extra = f'1\n\n{width} {height}\n1\n0 0 {width} {height} 0 0 0 {"frame0" if named else ""}\n'.encode()
    chunk = b'extr'+struct.pack('<I',len(extra))+extra+b'\0'*(len(extra)%2)
    return raw[:4]+struct.pack('<I',len(raw)+len(chunk)-8)+raw[8:]+chunk


def battle_loading_sprite(image):
    # LevelImage expands square textures to a centered 4:3 rectangle before
    # clipping them to the viewport. Store the matching anamorphic frame.
    frame = ImageOps.fit(image, (1600, 1200), method=Image.Resampling.LANCZOS)
    size = (1024, 1024)
    return sprite(frame.resize(size, Image.Resampling.LANCZOS), size)


def texture(image, *, full_color=False):
    """Native PVR v3 with a complete, independently filtered mip chain."""
    image = image.convert('RGBA')
    width,height = image.size
    if any(side & (side-1) for side in image.size):
        raise ValueError('Expected power-of-two map texture dimensions')
    levels = max(width,height).bit_length()
    pixel_format = b'rgba' + bytes([8 if full_color else 4])*4
    header = struct.pack('<IIQ9I',0x03525650,0,int.from_bytes(pixel_format,'little'),
                         0,0,height,width,1,1,1,levels,0)
    surfaces = []
    for _ in range(levels):
        # Resize individual channels to preserve the fourth material weight
        # where a conventional RGBA resampler would premultiply by alpha.
        if full_color:
            surfaces.append(image.tobytes())
        else:
            channels = [np.asarray(channel,dtype='<u2') >> 4 for channel in image.split()]
            pixels = (channels[0]<<12) | (channels[1]<<8) | (channels[2]<<4) | channels[3]
            surfaces.append(pixels.astype('<u2',copy=False).tobytes())
        if width > 1 or height > 1:
            width,height = max(1,width//2),max(1,height//2)
            image = Image.merge('RGBA',tuple(c.resize((width,height),Image.Resampling.BOX) for c in image.split()))
    return header + b''.join(surfaces)


def repeated_patch(values):
    # Overlap 64 source pixels at the top and left tile edges. The output
    # crosses each periodic boundary through adjacent original pixels.
    blend = np.linspace(0,1,64,dtype=np.float32)
    if values.ndim == 3:
        blend = blend[:,None]
    values[:,:64] = values[:,512:]*(1-blend) + values[:,:64]*blend
    values[:64] = values[512:]*(1-blend[:,None]) + values[:64]*blend[:,None]
    return values[:512,:512]


def surface_detail():
    """Pack four independent, seamless luminance swatches for the landscape shader."""
    from PIL import ImageFilter

    channels = []
    with Image.open(ROOT/'solbreaker/assets/proving-ground-surfaces.webp') as atlas:
        width, height = atlas.width//2, atlas.height//2
        for i, (mean, contrast) in enumerate(((134,24), (135,30), (134,28), (134,25))):
            x, y = (i%2)*width, (i//2)*height
            patch = atlas.crop((x+4,y+4,x+width-4,y+height-4)).convert('L')
            patch = patch.resize((576,576),Image.Resampling.LANCZOS)
            values = np.asarray(patch,dtype=np.float32)
            # Remove the baked illumination. Blend the spare 64 pixels on both
            # edges before cropping, keeping the result periodic in both axes.
            values -= .60 * np.asarray(patch.filter(ImageFilter.GaussianBlur(14)),dtype=np.float32)
            values = repeated_patch(values)
            values = (values-values.mean()) * (contrast/max(1.,values.std())) + mean
            channels.append(Image.fromarray(np.clip(values,0,255).astype(np.uint8)))
    return Image.merge('RGBA', channels)


def finishes():
    # The vertex stream carries the structure's base colour. Preserve the
    # colour of worn paint while keeping the local texture bright enough for
    # the unlit tinted material. Each patch repeats every four world metres.
    with Image.open(ROOT/'solbreaker/assets/proving-ground-structures.webp') as atlas:
        width,height = atlas.width//2,atlas.height//2
        for i,name in enumerate(('sheet','aggregate','plate','roof')):
            x,y = (i%2)*width,(i//2)*height
            patch = atlas.crop((x+4,y+4,x+width-4,y+height-4)).convert('RGB')
            values = np.array(patch.resize((576,576),Image.Resampling.LANCZOS),dtype=np.float32)
            values = repeated_patch(values)
            luminance = values.mean(axis=2,keepdims=True)
            chroma = values-luminance
            contrast = (luminance-luminance.mean()) * (42./max(1.,luminance.std()))
            rgb = np.clip(207. + contrast + chroma*.70, 70.,255.).astype(np.uint8)
            yield name,Image.fromarray(rgb)


def wayfinder(number, title):
    # A number and a single large label remain readable from the driver's
    # seat. Normalised UVs map onto a 9.6 x 3.8 m physical board.
    image = Image.new('RGB',(1024,400),(30,44,44))
    draw = ImageDraw.Draw(image)
    draw.rectangle((3,3,1020,396),outline=(180,188,173),width=5)
    draw.rectangle((11,11,275,388),fill=(150,49,35))
    draw.text((142,200),number,font=ImageFont.load_default(size=178),
              fill=(255,240,203),anchor='mm',stroke_width=2)
    font = ImageFont.load_default(size=155)
    if draw.textlength(title,font=font)>688:
        font = ImageFont.load_default(size=int(155*688/draw.textlength(title,font=font)))
    draw.text((649,199),title,font=font,fill=(249,244,222),anchor='mm',stroke_width=1)
    return image.resize((1024,512),Image.Resampling.LANCZOS)


def shaded_ground(geometry):
    """Bake contact and cast shadows only for fixed structures on the course."""
    from PIL import ImageFilter

    ground = layout.terrain_color(4096)
    size = ground.width
    shadows = Image.new('L',ground.size)
    brush = ImageDraw.Draw(shadows)
    for obj in geometry['objects']:
        if not obj.get('solid',True):
            continue
        vertices = np.asarray(obj['vertices'])
        base = layout.elevation(vertices[:,0], vertices[:,1])
        # Match the light coming from the upper left of the tank in the game.
        above = np.maximum(0.,vertices[:,2]-base)
        px = (vertices[:,0]+.53*above+512.)*(size/1024.)
        py = (512.-vertices[:,1]+.33*above)*(size/1024.)
        points = np.column_stack((px,py)).round().astype(int).tolist()
        for a,b,c in obj['triangles']:
            brush.polygon((tuple(points[a]),tuple(points[b]),tuple(points[c])),fill=255)
    shadow = np.asarray(shadows.filter(ImageFilter.GaussianBlur(1.4)),dtype=np.float32)
    pixels = np.asarray(ground,dtype=np.float32)*(1.-.28*shadow[:,:,None]/255.)
    return Image.fromarray(pixels.astype(np.uint8)).convert('RGBA')


def build(overview, card):
    geometry = layout.geometry()
    if geometry['axis'] != 'dava_z_up' or geometry['format'] != 'blitz-physics-geometry/v1':
        raise ValueError('Unsupported layout coordinates')
    # The native landscape UV origin is on the north edge. MKM row zero,
    # unlike these two images, samples the southern edge in server space.
    mask = layout.terrain_mask(1024)
    ground = shaded_ground(geometry)
    ground.putalpha(232)
    codes = np.argmax(np.asarray(mask),axis=2)[::-1,:].astype(np.uint8).reshape(-1,4)
    surface = codes[:,0] | (codes[:,1]<<2) | (codes[:,2]<<4) | (codes[:,3]<<6)
    resources = {
        PARENT/'landscape'/f'{KEY}.heightmap':layout.heightmap(),
        PARENT/'landscape/solbreaker_range_color.dx11.pvr':texture(ground,full_color=True),
        PARENT/'landscape/solbreaker_range_mask.dx11.pvr':texture(layout.terrain_mask(2048,blended=True)),
        PARENT/'blitz'/f'{KEY}.mkm':struct.pack('<4s5I',b'kkm\0',1,1024,len(surface),0,0)+surface.tobytes(),
        PARENT/'blitz'/f'{KEY}.yaml':('keyedArchive:\n'+''.join(
            f'    fx{i}:\n        string: "ground"\n' for i in range(4))).encode(),
    }
    resources[PARENT/'landscape/solbreaker_range_detail.dx11.pvr'] = texture(surface_detail(),full_color=True)
    for name,swatch in finishes():
        resources[PARENT/'landscape'/f'solbreaker_range_{name}.dx11.pvr'] = texture(swatch,full_color=True)
    for number,title,*_ in SIGNS:
        resources[PARENT/'landscape'/f'solbreaker_sign_{number}.dx11.pvr'] = texture(wayfinder(number,title),full_color=True)
    art = Path('Gfx/UI')
    overhead = layout.minimap(512)
    for suffix,scale in (('',1),('@2x',2)):
        resources[art/'BattleScreenHUD/minimap'/KEY/f'MiniMapSmall{suffix}.packed.webp'] = sprite(
            overhead,(252*scale,)*2)
        resources[art/'Hangar/GameTypes'/f'battle-type_{KEY}{suffix}.packed.webp'] = sprite(
            card,(250*scale,330*scale),named=False)
    resources[art/'BattleLoadingScreen'/KEY/'BackgroundLoading.packed.webp'] = battle_loading_sprite(overview)
    doc = dict(format='solbreaker-proving-ground/v1',objects=[*geometry['objects'], *sign_meshes(layout.elevation), *horizon_meshes()],
               resources={path.as_posix():base64.b64encode(data).decode('ascii')
                          for path,data in resources.items()})
    return gzip.compress(json.dumps(doc,separators=(',',':'),allow_nan=False).encode(),compresslevel=9,mtime=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--overview', type=Path, required=True)
    parser.add_argument('--card', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=BUNDLE)
    args = parser.parse_args()
    with Image.open(args.overview) as overview, Image.open(args.card) as card:
        args.output.write_bytes(build(overview,card))


if __name__ == '__main__':
    main()
