/**
 * Printer-specific 3D models for the viewport.
 *
 * Each supported machine gets its own model, built from primitives at the
 * machine's real proportions (frame, gantry, bed, enclosure, toolhead, screen),
 * so the viewport shows the printer that was actually selected rather than one
 * generic frame. The build volume always comes from the selected profile; the
 * outer dimensions of the machine are approximations of the published specs.
 *
 * Coordinates match the viewport: the build plate corner is the origin, +X is
 * right, +Y is toward the back of the printer, +Z is up, units are millimetres.
 * The plate surface is z = 0, so a model placed at z = 0 sits on the bed.
 */

import * as THREE from 'three'

export type PrinterKind = 'ender3' | 'mk4s' | 'bambu-x1c' | 'bambu-p1s' | 'voron-2-4'

export interface BuildVolumeMm {
  x: number
  y: number
  z: number
}

export interface PrinterModel {
  kind: PrinterKind
  title: string
  group: THREE.Group
  /** Bounds of the whole machine, used to frame the camera. */
  bounds: THREE.Box3
}

const TITLES: Record<PrinterKind, string> = {
  ender3: 'Creality Ender 3',
  mk4s: 'Prusa MK4S',
  'bambu-x1c': 'Bambu Lab X1 Carbon',
  'bambu-p1s': 'Bambu Lab P1S',
  'voron-2-4': 'Voron 2.4',
}

/** Maps the catalogue label ("Bambu Lab X1 Carbon") to a model, or null if we have none. */
export function identifyPrinter(label?: string | null): PrinterKind | null {
  if (!label) return null
  const text = label.toLowerCase()
  if (/ender[\s-]*3/.test(text)) return 'ender3'
  if (/mk\s*4/.test(text)) return 'mk4s'
  if (/voron/.test(text)) return 'voron-2-4'
  if (/\bp1[sp]\b/.test(text)) return 'bambu-p1s'
  if (/\bx1\b|x1c|x1[\s-]*carbon/.test(text)) return 'bambu-x1c'
  return null
}

// ---------------------------------------------------------------------------
// materials and a tiny primitive kit
// ---------------------------------------------------------------------------

function solid(color: number, roughness = 0.6, metalness = 0.15): THREE.MeshStandardMaterial {
  return new THREE.MeshStandardMaterial({ color, roughness, metalness })
}

function pane(color: number, opacity: number): THREE.MeshStandardMaterial {
  return new THREE.MeshStandardMaterial({
    color,
    roughness: 0.15,
    metalness: 0,
    transparent: true,
    opacity,
    depthWrite: false,
    side: THREE.DoubleSide,
  })
}

function palette() {
  return {
    frame: solid(0x1d1e21, 0.55, 0.35),
    black: solid(0x0c0c0e, 0.7, 0.1),
    darkGray: solid(0x383b40, 0.6, 0.2),
    steel: solid(0xb9bec4, 0.35, 0.8),
    orange: solid(0xf2641a, 0.55, 0.1),
    red: solid(0xc62828, 0.55, 0.1),
    gold: solid(0xc9a227, 0.35, 0.9),
    pei: solid(0x2c2b29, 0.9, 0.05),
    peiGold: solid(0x6d5f3d, 0.85, 0.1),
    screen: new THREE.MeshStandardMaterial({
      color: 0x0b2a4a,
      emissive: 0x0b2a4a,
      emissiveIntensity: 0.6,
      roughness: 0.3,
    }),
    glassClear: pane(0xaecbe0, 0.14),
    glassSmoke: pane(0x10161b, 0.34),
    sidePanel: pane(0x23262a, 0.5),
    lidPlastic: pane(0x25272b, 0.55),
    voronPanel: pane(0x9fb6c4, 0.1),
  }
}

type Palette = ReturnType<typeof palette>

class Kit {
  readonly group = new THREE.Group()

  constructor(
    readonly cx: number,
    readonly cy: number,
  ) {}

  /** Axis-aligned box from min corner to max corner. */
  box(
    material: THREE.Material,
    x0: number,
    y0: number,
    z0: number,
    x1: number,
    y1: number,
    z1: number,
    edge?: number,
  ): THREE.Mesh {
    const geometry = new THREE.BoxGeometry(Math.abs(x1 - x0), Math.abs(y1 - y0), Math.abs(z1 - z0))
    const mesh = new THREE.Mesh(geometry, material)
    mesh.position.set((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
    this.group.add(mesh)
    if (edge !== undefined) {
      const lines = new THREE.LineSegments(
        new THREE.EdgesGeometry(geometry),
        new THREE.LineBasicMaterial({ color: edge, transparent: true, opacity: 0.55 }),
      )
      lines.position.copy(mesh.position)
      this.group.add(lines)
    }
    return mesh
  }

  /** Cylinder along an axis, centred on (x, y, z). */
  cyl(
    material: THREE.Material,
    axis: 'x' | 'y' | 'z',
    x: number,
    y: number,
    z: number,
    radius: number,
    length: number,
  ): THREE.Mesh {
    const mesh = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, length, 20), material)
    if (axis === 'x') mesh.rotation.z = Math.PI / 2
    if (axis === 'z') mesh.rotation.x = Math.PI / 2
    mesh.position.set(x, y, z)
    this.group.add(mesh)
    return mesh
  }
}

/** Heated bed + print surface, top of the surface at z = -0.3 so parts sit on it. */
function bed(
  k: Kit,
  p: Palette,
  width: number,
  depth: number,
  surface: THREE.Material,
  thickness = 7,
): void {
  const x0 = k.cx - width / 2
  const x1 = k.cx + width / 2
  const y0 = k.cy - depth / 2
  const y1 = k.cy + depth / 2
  k.box(surface, x0, y0, -2.5, x1, y1, -0.3)
  k.box(p.steel, x0, y0, -2.5 - thickness, x1, y1, -2.5)
}

// ---------------------------------------------------------------------------
// Creality Ender 3 - open cartesian bed-slinger, aluminium extrusion frame
// ---------------------------------------------------------------------------

function ender3(k: Kit, p: Palette, v: BuildVolumeMm): void {
  const { cx, cy } = k
  const nozzleZ = v.z + 10
  const baseZ = -70
  const top = baseZ + 465
  const bedX = v.x + 15
  const bedY = v.y + 15

  bed(k, p, bedX, bedY, p.pei, 5.5)
  k.box(p.darkGray, cx - bedX / 2, cy - bedY / 2, -14, cx + bedX / 2, cy + bedY / 2, -8)
  for (const sx of [-1, 1]) {
    for (const sy of [-1, 1]) {
      k.cyl(p.black, 'z', cx + sx * (bedX / 2 - 20), cy + sy * (bedY / 2 - 20), -22, 10, 16)
    }
  }

  // base beam (Y axis), cross tie, uprights, top brace
  k.box(p.frame, cx - 40, cy - v.y / 2 - 110, baseZ, cx + 40, cy + v.y / 2 + 110, -30)
  k.box(p.frame, cx - 165, cy + 40, baseZ, cx + 165, cy + 80, -30)
  k.box(p.frame, cx - 165, cy + 40, baseZ, cx - 145, cy + 80, top)
  k.box(p.frame, cx + 145, cy + 40, baseZ, cx + 165, cy + 80, top)
  k.box(p.frame, cx - 165, cy + 50, top - 20, cx + 165, cy + 70, top)

  // Z lead screw + motor
  k.cyl(p.steel, 'z', cx - 152, cy + 25, (top - 20 - 30) / 2, 4, top - 20 + 30)
  k.box(p.darkGray, cx - 173, cy + 4, baseZ, cx - 131, cy + 46, baseZ + 47)

  // X gantry, motor, hotend
  k.box(p.frame, cx - 165, cy + 20, nozzleZ + 30, cx + 165, cy + 40, nozzleZ + 70)
  k.box(p.steel, cx - 172, cy + 18, nozzleZ + 25, cx - 140, cy + 42, nozzleZ + 75)
  k.box(p.steel, cx + 140, cy + 18, nozzleZ + 25, cx + 172, cy + 42, nozzleZ + 75)
  k.box(p.darkGray, cx + 172, cy + 18, nozzleZ + 28, cx + 214, cy + 60, nozzleZ + 70)
  k.box(p.darkGray, cx + 165, cy + 40, top - 80, cx + 207, cy + 82, top - 38)
  k.box(p.darkGray, cx - 24, cy - 18, nozzleZ + 10, cx + 24, cy + 20, nozzleZ + 75)
  k.box(p.black, cx - 20, cy - 30, nozzleZ + 8, cx + 20, cy - 10, nozzleZ + 40)
  k.box(p.gold, cx - 8, cy - 8, nozzleZ + 3, cx + 8, cy + 8, nozzleZ + 10)
  k.cyl(p.gold, 'z', cx, cy, nozzleZ + 1.5, 3, 3)

  // control box with the LCD and knob, power supply
  const front = cy - v.y / 2 - 110
  k.box(p.darkGray, cx + 10, front, baseZ + 4, cx + 118, front + 102, -6)
  k.box(p.screen, cx + 22, front - 2, -46, cx + 88, front, -22)
  k.cyl(p.steel, 'y', cx + 102, front - 4, -34, 8, 8)
  k.box(p.steel, cx + 40, cy + 120, baseZ, cx + 100, cy + 215, -20)
}

// ---------------------------------------------------------------------------
// Prusa MK4S - open i3 bed-slinger, black frame, orange printed parts
// ---------------------------------------------------------------------------

function mk4s(k: Kit, p: Palette, v: BuildVolumeMm): void {
  const { cx, cy } = k
  const nozzleZ = v.z + 10
  const baseZ = -52
  const top = v.z + 120

  bed(k, p, v.x + 8, v.y + 8, p.pei, 7)
  k.box(p.frame, k.cx - (v.x + 8) / 2, cy - (v.y + 8) / 2, -16, cx + (v.x + 8) / 2, cy + (v.y + 8) / 2, -9.5)

  // base: two Y beams, front/rear ties, post tie
  const yFront = cy - v.y / 2 - 90
  const yRear = cy + v.y / 2 + 130
  k.box(p.frame, cx - 100, yFront, baseZ, cx - 80, yRear, -22)
  k.box(p.frame, cx + 80, yFront, baseZ, cx + 100, yRear, -22)
  k.box(p.frame, cx - 100, yFront, baseZ, cx + 100, yFront + 20, -22)
  k.box(p.frame, cx - 100, yRear - 20, baseZ, cx + 100, yRear, -22)
  k.box(p.frame, cx - 160, cy + 55, baseZ, cx + 160, cy + 95, -22)

  // Z posts, lead screws
  k.box(p.frame, cx - 160, cy + 55, baseZ, cx - 140, cy + 95, top)
  k.box(p.frame, cx + 140, cy + 55, baseZ, cx + 160, cy + 95, top)
  for (const side of [-1, 1]) {
    k.cyl(p.steel, 'z', cx + side * 150, cy + 45, (top - 10 - 22) / 2, 4, top - 10 + 22)
  }

  // X axis with orange X-ends
  k.box(p.frame, cx - 150, cy + 30, nozzleZ + 30, cx + 150, cy + 50, nozzleZ + 62)
  k.box(p.orange, cx - 166, cy + 22, nozzleZ + 20, cx - 126, cy + 66, nozzleZ + 72)
  k.box(p.orange, cx + 126, cy + 22, nozzleZ + 20, cx + 166, cy + 66, nozzleZ + 72)

  // Nextruder: black body, orange fan shroud, heater block, nozzle
  k.box(p.black, cx - 28, cy - 22, nozzleZ + 8, cx + 28, cy + 30, nozzleZ + 74)
  k.box(p.orange, cx - 22, cy - 40, nozzleZ + 10, cx + 22, cy - 8, nozzleZ + 46)
  k.box(p.gold, cx - 8, cy - 8, nozzleZ + 3, cx + 8, cy + 8, nozzleZ + 10)
  k.cyl(p.gold, 'z', cx, cy, nozzleZ + 1.5, 3, 3)

  // LCD tower, front right
  const lcdY = cy - v.y / 2 - 60
  k.box(p.frame, cx + 100, lcdY, baseZ, cx + 175, lcdY + 45, -22)
  k.box(p.black, cx + 135, lcdY, -22, cx + 175, lcdY + 45, 40)
  k.box(p.screen, cx + 140, lcdY - 2, 2, cx + 170, lcdY, 32)

  // power supply, rear
  k.box(p.black, cx - 60, cy + v.y / 2 + 140, -22, cx + 60, cy + v.y / 2 + 225, 45)
}

// ---------------------------------------------------------------------------
// Bambu Lab X1 Carbon / P1S - enclosed CoreXY, 389 x 389 x 457 mm body
// ---------------------------------------------------------------------------

function bambu(k: Kit, p: Palette, v: BuildVolumeMm, variant: 'x1c' | 'p1s'): void {
  const { cx, cy } = k
  const hx = v.x / 2 + 66.5
  const hy = v.y / 2 + 66.5
  const zb = -72
  const zt = v.z + 129
  const x0 = cx - hx
  const x1 = cx + hx
  const y0 = cy - hy
  const y1 = cy + hy
  const nozzleZ = v.z + 10
  const edge = 0x0a0a0a

  // plinth and corner posts
  k.box(p.frame, x0, y0, zb, x1, y1, zb + 14)
  for (const [px, py] of [
    [x0, y0],
    [x1 - 16, y0],
    [x0, y1 - 16],
    [x1 - 16, y1 - 16],
  ] as const) {
    k.box(p.frame, px, py, zb, px + 16, py + 16, zt)
  }
  for (const z of [zt - 14]) {
    k.box(p.frame, x0, y0, z, x1, y0 + 16, zt)
    k.box(p.frame, x0, y1 - 16, z, x1, y1, zt)
    k.box(p.frame, x0, y0, z, x0 + 16, y1, zt)
    k.box(p.frame, x1 - 16, y0, z, x1, y1, zt)
  }

  // shell: side and back panels, front door, top lid
  k.box(p.sidePanel, x0, y0 + 16, zb + 14, x0 + 3, y1 - 16, zt - 14, edge)
  k.box(p.sidePanel, x1 - 3, y0 + 16, zb + 14, x1, y1 - 16, zt - 14, edge)
  k.box(p.sidePanel, x0 + 16, y1 - 3, zb + 14, x1 - 16, y1, zt - 14, edge)
  const door = variant === 'x1c' ? p.glassSmoke : p.glassClear
  const lid = variant === 'x1c' ? p.glassSmoke : p.lidPlastic
  k.box(door, x0 + 16, y0, zb + 44, x1 - 16, y0 + 3, zt - 14, edge)
  k.box(lid, x0 + 16, y0 + 16, zt - 4, x1 - 16, y1 - 16, zt, edge)
  k.box(p.frame, x0 + 16, y0, zb + 14, x1 - 16, y0 + 4, zb + 44) // lower front panel
  k.box(p.black, x0 + 24, y0 - 5, zb + 120, x0 + 29, y0 + 1, zb + 180) // door handle

  // touch screen, lower right of the front panel
  k.box(p.screen, x1 - 100, y0 - 2, zb + 20, x1 - 40, y0, zb + 40)

  // CoreXY gantry: Y rods, X rod, toolhead
  const railX = v.x / 2 + 22
  for (const side of [-1, 1]) {
    k.cyl(p.steel, 'y', cx + side * railX, cy, nozzleZ + 38, 4, v.y + 40)
  }
  k.cyl(p.darkGray, 'x', cx, cy + 20, nozzleZ + 42, 5, railX * 2)
  k.box(p.frame, cx - 28, cy - 30, nozzleZ + 4, cx + 28, cy + 30, nozzleZ + 64)
  k.box(p.black, cx - 20, cy - 36, nozzleZ + 10, cx + 20, cy - 28, nozzleZ + 40)
  k.box(p.gold, cx - 7, cy - 7, nozzleZ + 3, cx + 7, cy + 7, nozzleZ + 9)
  k.cyl(p.gold, 'z', cx, cy, nozzleZ + 1.5, 3, 3)
  for (const side of [-1, 1]) {
    k.box(p.frame, cx + side * (hx - 40) - 21, y1 - 60, nozzleZ + 30, cx + side * (hx - 40) + 21, y1 - 18, nozzleZ + 70)
  }

  // heated bed on its rear arm, with the lead screws at the back
  bed(k, p, v.x + 1, v.y + 1, p.peiGold, 8)
  k.box(p.frame, cx - 110, cy, -32, cx + 110, y1 - 6, -18)
  k.box(p.darkGray, cx - 100, cy - v.y / 2, -18, cx + 100, cy + v.y / 2, -10.5)
  for (const side of [-1, 1]) {
    k.cyl(p.steel, 'z', cx + side * 110, y1 - 14, (zb + 14 + zt - 20) / 2, 4, zt - 20 - zb - 14)
  }
}

// ---------------------------------------------------------------------------
// Voron 2.4 (350) - enclosed CoreXY cube, extrusion frame, red toolhead
// ---------------------------------------------------------------------------

function voron24(k: Kit, p: Palette, v: BuildVolumeMm): void {
  const { cx, cy } = k
  const hx = v.x / 2 + 75
  const hy = v.y / 2 + 85
  const zb = -85
  const zt = v.z + 100
  const x0 = cx - hx
  const x1 = cx + hx
  const y0 = cy - hy
  const y1 = cy + hy
  const nozzleZ = v.z + 10
  const edge = 0x4a5a66

  // 2020 frame: posts, top and bottom rings
  for (const [px, py] of [
    [x0, y0],
    [x1 - 20, y0],
    [x0, y1 - 20],
    [x1 - 20, y1 - 20],
  ] as const) {
    k.box(p.frame, px, py, zb, px + 20, py + 20, zt)
  }
  for (const [za, zbb] of [
    [zb, zb + 20],
    [zt - 20, zt],
  ] as const) {
    k.box(p.frame, x0, y0, za, x1, y0 + 20, zbb)
    k.box(p.frame, x0, y1 - 20, za, x1, y1, zbb)
    k.box(p.frame, x0, y0, za, x0 + 20, y1, zbb)
    k.box(p.frame, x1 - 20, y0, za, x1, y1, zbb)
  }
  k.box(p.black, x0 + 20, y0 + 20, zb, x1 - 20, y1 - 20, zb + 8) // floor

  // panels: sides, back, split front doors, top
  k.box(p.voronPanel, x0, y0 + 20, zb + 20, x0 + 3, y1 - 20, zt - 20, edge)
  k.box(p.voronPanel, x1 - 3, y0 + 20, zb + 20, x1, y1 - 20, zt - 20, edge)
  k.box(p.voronPanel, x0 + 20, y1 - 3, zb + 20, x1 - 20, y1, zt - 20, edge)
  k.box(p.voronPanel, x0 + 20, y0, zb + 20, cx - 1.5, y0 + 3, zt - 20, edge)
  k.box(p.voronPanel, cx + 1.5, y0, zb + 20, x1 - 20, y0 + 3, zt - 20, edge)
  k.box(p.voronPanel, x0 + 20, y0 + 20, zt - 3, x1 - 20, y1 - 20, zt, edge)
  k.box(p.black, cx - 14, y0 - 8, zb + 150, cx - 8, y0, zb + 330) // door handles
  k.box(p.black, cx + 8, y0 - 8, zb + 150, cx + 14, y0, zb + 330)

  // four Z lead screws in the corners
  for (const sx of [-1, 1]) {
    for (const sy of [-1, 1]) {
      k.cyl(
        p.steel,
        'z',
        cx + sx * (v.x / 2 + 28),
        cy + sy * (v.y / 2 + 32),
        (zb + 20 + zt - 20) / 2,
        4,
        zt - 20 - zb - 20,
      )
    }
  }

  // bed: carrier, aluminium plate, PEI sheet
  k.box(p.frame, cx - v.x / 2, cy - v.y / 2, -30, cx + v.x / 2, cy + v.y / 2, -12)
  bed(k, p, v.x, v.y, p.pei, 9.5)

  // gantry: Y rails, X extrusion, motors, Stealthburner toolhead
  const railX = v.x / 2 + 38
  for (const side of [-1, 1]) {
    k.box(p.frame, cx + side * railX - 10, y0 + 30, nozzleZ + 45, cx + side * railX + 10, y1 - 30, nozzleZ + 65)
  }
  k.box(p.frame, cx - railX - 10, cy + 12, nozzleZ + 38, cx + railX + 10, cy + 36, nozzleZ + 58)
  k.box(p.black, cx - 26, cy - 28, nozzleZ + 6, cx + 26, cy + 34, nozzleZ + 64)
  k.box(p.red, cx - 20, cy - 38, nozzleZ + 22, cx + 20, cy - 12, nozzleZ + 56)
  k.box(p.gold, cx - 7, cy - 7, nozzleZ + 3, cx + 7, cy + 7, nozzleZ + 9)
  k.cyl(p.gold, 'z', cx, cy, nozzleZ + 1.5, 3, 3)
  for (const side of [-1, 1]) {
    k.box(p.darkGray, cx + side * (hx - 40) - 21, y1 - 70, zt - 62, cx + side * (hx - 40) + 21, y1 - 28, zt - 22)
  }
}

// ---------------------------------------------------------------------------

export function buildPrinterModel(kind: PrinterKind, volume: BuildVolumeMm): PrinterModel {
  const kit = new Kit(volume.x / 2, volume.y / 2)
  const pal = palette()
  switch (kind) {
    case 'ender3':
      ender3(kit, pal, volume)
      break
    case 'mk4s':
      mk4s(kit, pal, volume)
      break
    case 'bambu-x1c':
      bambu(kit, pal, volume, 'x1c')
      break
    case 'bambu-p1s':
      bambu(kit, pal, volume, 'p1s')
      break
    case 'voron-2-4':
      voron24(kit, pal, volume)
      break
  }
  const bounds = new THREE.Box3().setFromObject(kit.group)
  return { kind, title: TITLES[kind], group: kit.group, bounds }
}

export function disposePrinterModel(model: PrinterModel): void {
  model.group.traverse((object) => {
    if (object instanceof THREE.Mesh || object instanceof THREE.LineSegments) {
      object.geometry.dispose()
      const material = object.material
      if (Array.isArray(material)) material.forEach((m) => m.dispose())
      else material.dispose()
    }
  })
}
