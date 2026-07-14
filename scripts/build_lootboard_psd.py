"""Build a layered PSD of the DropTracker lootboard template.

Sources (all 1074x795, in disc/lootboard/):
  bank-new-clean-dark.png  - full template (target of flatten-fidelity check)
  no_boxes_dark.png        - same, without slot/submission boxes
  no_boxes_minimal.png     - same, also without the chest artwork

Layers are derived by pixel-diffing the three, plus region cuts of the
remaining elements (table, texts) with same-row texture-copied infill.
Flattened output is asserted pixel-identical to bank-new-clean-dark.png.
"""
import os
import numpy as np
from PIL import Image
from scipy import ndimage

from pytoshop import enums
from pytoshop.user import nested_layers

D = '/store/droptracker/disc/lootboard/'
OUT = D + 'lootboard-template.psd'

full = np.array(Image.open(D + 'bank-new-clean-dark.png').convert('RGBA'), dtype=np.uint8)
noboxes = np.array(Image.open(D + 'no_boxes_dark.png').convert('RGBA'), dtype=np.uint8)
minimal = np.array(Image.open(D + 'no_boxes_minimal.png').convert('RGBA'), dtype=np.uint8)
H, W = full.shape[:2]

background = minimal.copy()

def cut_element(search_box, name, src_x, thresh=1, pad=2):
    """Lift the element inside search_box (y0,y1,x0,x1) of `minimal` onto its
    own layer. Detection: per-pixel max channel delta vs a clean patch of the
    same rows starting at src_x (>= thresh), dilated by pad. Background gets
    infilled with the clean-patch pixels so texture is preserved."""
    y0, y1, x0, x1 = search_box
    region = minimal[y0:y1, x0:x1].astype(int)
    src = minimal[y0:y1, src_x:src_x + (x1 - x0)].astype(int)
    ref = np.median(src, axis=1, keepdims=True)  # per-row background color
    delta = np.abs(region - ref).max(axis=2)
    mask = ndimage.binary_dilation(delta >= thresh, iterations=pad)
    if not mask.any():
        raise RuntimeError(f'{name}: nothing found')
    layer = np.zeros((y1 - y0, x1 - x0, 4), dtype=np.uint8)
    layer[mask] = minimal[y0:y1, x0:x1][mask]
    bg_region = background[y0:y1, x0:x1]
    bg_region[mask] = src.astype(np.uint8)[mask]
    ys, xs = np.where(mask)
    by0, by1, bx0, bx1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    print(f'{name}: x={x0+bx0}-{x0+bx1} y={y0+by0}-{y0+by1} px={mask.sum()}')
    return dict(name=name, top=y0 + int(by0), left=x0 + int(bx0),
                pixels=layer[by0:by1, bx0:bx1])

def diff_layer(a, b, name):
    """Layer = pixels of `a` where a differs from `b`."""
    mask = np.any(a != b, axis=2)
    ys, xs = np.where(mask)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    layer = np.zeros((y1 - y0, x1 - x0, 4), dtype=np.uint8)
    sub = mask[y0:y1, x0:x1]
    layer[sub] = a[y0:y1, x0:x1][sub]
    return dict(name=name, top=int(y0), left=int(x0), pixels=layer)

# ---- 1. Chest artwork (noboxes vs minimal) ----
art = diff_layer(noboxes, minimal, 'Chest artwork')

# ---- 2. Boxes (full vs noboxes), one layer per box ----
boxmask = np.any(full != noboxes, axis=2)
lab, n = ndimage.label(boxmask)
slots, subs = [], []
for i, sl in enumerate(ndimage.find_objects(lab)):
    y0, y1 = sl[0].start, sl[0].stop
    x0, x1 = sl[1].start, sl[1].stop
    layer = np.zeros((y1 - y0, x1 - x0, 4), dtype=np.uint8)
    m = lab[sl] == i + 1
    layer[m] = full[sl][m]
    if y0 < 500:  # 4x8 item slot grid, 76x76 on 90px pitch
        r, c = (y0 - 188) // 78 + 1, (x0 - 311) // 90 + 1
        slots.append(dict(name=f'Slot R{r}C{c}', top=y0, left=x0, pixels=layer))
    else:         # 2x6 recent-submission boxes, 142x76
        r = 1 if y0 < 620 else 2
        c = len([s for s in subs if s['name'].startswith(f'Box R{r}')]) + 1
        subs.append(dict(name=f'Box R{r}C{c}', top=y0, left=x0, pixels=layer))
print(f'slots: {len(slots)}, submission boxes: {len(subs)}')

# ---- 3. Region cuts from minimal ----
# panel is perfectly flat behind these three -> exact diff (thresh=1)
icon = cut_element((58, 126, 60, 280), 'Chest hand icon', src_x=320)
table = cut_element((125, 496, 11, 300), 'Top Looters table', src_x=320)
rs_text = cut_element((495, 532, 420, 740), 'Recent Submissions text', src_x=90)
# footer bar has +-5 noise texture -> contrast threshold
foot_l = cut_element((758, 792, 12, 165), 'DropTracker.io text', src_x=300, thresh=25)
foot_r = cut_element((758, 792, 905, 1068), '@joelhalen + Discord icon', src_x=300, thresh=25)

# ---- Build nested layer tree (first item = top of layer stack) ----
def img_layer(d):
    px = d['pixels']
    return nested_layers.Image(
        name=d['name'], visible=True, opacity=255,
        top=int(d['top']), left=int(d['left']),
        channels={0: px[..., 0], 1: px[..., 1], 2: px[..., 2], -1: px[..., 3]})

def group(name, items):
    return nested_layers.Group(name=name, visible=True, opacity=255, layers=items)

bg = dict(name='Background', top=0, left=0, pixels=background)

# NB: the slot/submission boxes were drawn over the "Recent Submissions"
# text in the original, so the text layer must sit below the box groups.
tree = [
    group('Footer', [img_layer(foot_l), img_layer(foot_r)]),
    group('Item Slots', [img_layer(s) for s in slots]),
    group('Recent Submissions', [img_layer(s) for s in subs] + [img_layer(rs_text)]),
    group('Top Looters', [img_layer(icon), img_layer(table)]),
    img_layer(art),
    img_layer(bg),
]

psd = nested_layers.nested_layers_to_psd(
    tree, color_mode=enums.ColorMode.rgb,
    version=enums.Version.psd, compression=enums.Compression.rle,
    size=(W, H))

# ---- Flatten-fidelity check (alpha-over compositing, bottom-up) ----
canvas = np.zeros((H, W, 4), dtype=np.uint8)

def paint(d):
    px = d['pixels']; t, l = d['top'], d['left']
    h, w = px.shape[:2]
    dst = canvas[t:t + h, l:l + w]
    a = px[..., 3:4].astype(np.uint16)
    out = ((px.astype(np.uint16) * a + dst.astype(np.uint16) * (255 - a)) // 255).astype(np.uint8)
    out[..., 3] = np.maximum(dst[..., 3], px[..., 3])
    dst[...] = out

for d in [bg, art, icon, table, rs_text] + subs + slots + [foot_l, foot_r]:
    paint(d)

if np.array_equal(canvas, full):
    print('FLATTEN CHECK: pixel-identical to bank-new-clean-dark.png')
else:
    bad = np.any(canvas != full, axis=2)
    ys, xs = np.where(bad)
    print(f'FLATTEN CHECK FAILED: {bad.sum()} px differ, '
          f'bbox x {xs.min()}-{xs.max()} y {ys.min()}-{ys.max()}')
    raise SystemExit(1)

# embed the verified flatten as the merged-image preview (for thumbnails
# and viewers that read the preview instead of compositing layers)
from pytoshop.image_data import ImageData
psd.image_data = ImageData(channels=np.ascontiguousarray(
    canvas.transpose(2, 0, 1)[[0, 1, 2]]), fd=None)

with open(OUT, 'wb') as f:
    psd.write(f)
print('wrote', OUT, os.path.getsize(OUT), 'bytes')
