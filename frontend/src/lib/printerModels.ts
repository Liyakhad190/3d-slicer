/**
 * Printer-specific 3D models for the viewport with real-time kinematics.
 *
 * Each supported machine gets its own model, built from primitives at the
 * machine's real proportions (frame, gantry, bed, enclosure, toolhead, screen).
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

export interface PrinterKinematics {
  /** Update toolhead / nozzle position in build plate space (0..buildVolume.x, 0..buildVolume.y, 0..buildVolume.z) */
  setNozzlePosition: (x: number, y: number, z: number, isExtruding?: boolean) => void
  /** Reset to default home position */
  home: () => void
  /** Get current nozzle position */
  getNozzlePosition: () => { x: number; y: number; z: number }
}

export interface PrinterModel {
  kind: PrinterKind
  title: string
  group: THREE.Group
  /** Bounds of the whole machine, used to frame the camera. */
  bounds: THREE.Box3
  kinematics?: PrinterKinematics
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
  private targetGroup: THREE.Group = this.group

  constructor(
    readonly cx: number,
    readonly cy: number,
  ) {}

  withGroup<T>(group: THREE.Group, fn: () => T): T {
    const prev = this.targetGroup
    this.targetGroup = group
    try {
      return fn()
    } finally {
      this.targetGroup = prev
    }
  }

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
    this.targetGroup.add(mesh)
    if (edge !== undefined) {
      const lines = new THREE.LineSegments(
        new THREE.EdgesGeometry(geometry),
        new THREE.LineBasicMaterial({ color: edge, transparent: true, opacity: 0.55 }),
      )
      lines.position.copy(mesh.position)
      this.targetGroup.add(lines)
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
    this.targetGroup.add(mesh)
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

function ender3(k: Kit, p: Palette, v: BuildVolumeMm): PrinterKinematics {
  const { cx, cy } = k
  const defaultNozzleZ = v.z + 10
  const baseZ = -70
  const top = baseZ + 465
  const bedX = v.x + 15
  const bedY = v.y + 15

  // Moving Bed
  const bedGroup = new THREE.Group()
  k.group.add(bedGroup)
  k.withGroup(bedGroup, () => {
    bed(k, p, bedX, bedY, p.pei, 5.5)
    k.box(p.darkGray, cx - bedX / 2, cy - bedY / 2, -14, cx + bedX / 2, cy + bedY / 2, -8)
    for (const sx of [-1, 1]) {
      for (const sy of [-1, 1]) {
        k.cyl(p.black, 'z', cx + sx * (bedX / 2 - 20), cy + sy * (bedY / 2 - 20), -22, 10, 16)
      }
    }
  })

  // Base frame, uprights & top brace (static)
  k.box(p.frame, cx - 40, cy - v.y / 2 - 110, baseZ, cx + 40, cy + v.y / 2 + 110, -30)
  k.box(p.frame, cx - 165, cy + 40, baseZ, cx + 165, cy + 80, -30)
  k.box(p.frame, cx - 165, cy + 40, baseZ, cx - 145, cy + 80, top)
  k.box(p.frame, cx + 145, cy + 40, baseZ, cx + 165, cy + 80, top)
  k.box(p.frame, cx - 165, cy + 50, top - 20, cx + 165, cy + 70, top)

  // Z lead screw + motor
  k.cyl(p.steel, 'z', cx - 152, cy + 25, (top - 20 - 30) / 2, 4, top - 20 + 30)
  k.box(p.darkGray, cx - 173, cy + 4, baseZ, cx - 131, cy + 46, baseZ + 47)

  // Control box with LCD & power supply
  const front = cy - v.y / 2 - 110
  k.box(p.darkGray, cx + 10, front, baseZ + 4, cx + 118, front + 102, -6)
  k.box(p.screen, cx + 22, front - 2, -46, cx + 88, front, -22)
  k.cyl(p.steel, 'y', cx + 102, front - 4, -34, 8, 8)
  k.box(p.steel, cx + 40, cy + 120, baseZ, cx + 100, cy + 215, -20)

  // Moving Z Gantry (moves vertically along Z)
  const zGantry = new THREE.Group()
  k.group.add(zGantry)

  k.withGroup(zGantry, () => {
    // X gantry rail and brackets
    k.box(p.frame, cx - 165, cy + 20, 30, cx + 165, cy + 40, 70)
    k.box(p.steel, cx - 172, cy + 18, 25, cx - 140, cy + 42, 75)
    k.box(p.steel, cx + 140, cy + 18, 25, cx + 172, cy + 42, 75)
    k.box(p.darkGray, cx + 172, cy + 18, 28, cx + 214, cy + 60, 70)
    k.box(p.darkGray, cx + 165, cy + 40, 50, cx + 207, cy + 82, 85)
  })

  // Moving Toolhead Carriage (moves along X rail within zGantry)
  const xCarriage = new THREE.Group()
  zGantry.add(xCarriage)

  let glowMesh: THREE.Mesh | null = null
  let nozzleLight: THREE.PointLight | null = null

  k.withGroup(xCarriage, () => {
    // Toolhead body, fan shroud & hotend
    k.box(p.darkGray, cx - 24, cy - 18, 10, cx + 24, cy + 20, 75)
    k.box(p.black, cx - 20, cy - 30, 8, cx + 20, cy - 10, 40)
    k.box(p.gold, cx - 8, cy - 8, 3, cx + 8, cy + 8, 10)
    k.cyl(p.gold, 'z', cx, cy, 1.5, 3, 3) // tip at z = 0

    // Molten nozzle tip indicator
    const glowGeo = new THREE.SphereGeometry(1.2, 8, 8)
    const glowMat = new THREE.MeshBasicMaterial({ color: 0x38bdf8, transparent: true, opacity: 0.9 })
    glowMesh = new THREE.Mesh(glowGeo, glowMat)
    glowMesh.position.set(cx, cy, 0.4)
    xCarriage.add(glowMesh)

    nozzleLight = new THREE.PointLight(0x38bdf8, 1.2, 35)
    nozzleLight.position.set(cx, cy, 1.0)
    xCarriage.add(nozzleLight)
  })

  let curX = cx
  let curY = cy
  let curZ = defaultNozzleZ

  const kinematics: PrinterKinematics = {
    setNozzlePosition(x: number, y: number, z: number, isExtruding = true) {
      curX = x
      curY = y
      curZ = z
      zGantry.position.z = z
      xCarriage.position.x = x - cx
      xCarriage.position.y = y - cy
      bedGroup.position.y = (cy - y) * 0.25
      if (nozzleLight) {
        nozzleLight.color.setHex(isExtruding ? 0x38bdf8 : 0xff7043)
        nozzleLight.intensity = isExtruding ? 1.4 : 0.3
      }
      if (glowMesh) {
        ;(glowMesh.material as THREE.MeshBasicMaterial).color.setHex(isExtruding ? 0x38bdf8 : 0xff7043)
      }
    },
    home() {
      kinematics.setNozzlePosition(cx, cy, defaultNozzleZ, false)
    },
    getNozzlePosition() {
      return { x: curX, y: curY, z: curZ }
    },
  }

  kinematics.home()
  return kinematics
}

// ---------------------------------------------------------------------------
// Prusa MK4S - open i3 bed-slinger, black frame, orange printed parts
// ---------------------------------------------------------------------------

function mk4s(k: Kit, p: Palette, v: BuildVolumeMm): PrinterKinematics {
  const { cx, cy } = k
  const defaultNozzleZ = v.z + 10
  const baseZ = -52
  const top = v.z + 120

  const bedGroup = new THREE.Group()
  k.group.add(bedGroup)
  k.withGroup(bedGroup, () => {
    bed(k, p, v.x + 8, v.y + 8, p.pei, 7)
    k.box(p.frame, cx - (v.x + 8) / 2, cy - (v.y + 8) / 2, -16, cx + (v.x + 8) / 2, cy + (v.y + 8) / 2, -9.5)
  })

  // Base frame
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

  // LCD & Power Supply
  const lcdY = cy - v.y / 2 - 60
  k.box(p.frame, cx + 100, lcdY, baseZ, cx + 175, lcdY + 45, -22)
  k.box(p.black, cx + 135, lcdY, -22, cx + 175, lcdY + 45, 40)
  k.box(p.screen, cx + 140, lcdY - 2, 2, cx + 170, lcdY, 32)
  k.box(p.black, cx - 60, cy + v.y / 2 + 140, -22, cx + 60, cy + v.y / 2 + 225, 45)

  // Z Gantry
  const zGantry = new THREE.Group()
  k.group.add(zGantry)
  k.withGroup(zGantry, () => {
    k.box(p.frame, cx - 150, cy + 30, 30, cx + 150, cy + 50, 62)
    k.box(p.orange, cx - 166, cy + 22, 20, cx - 126, cy + 66, 72)
    k.box(p.orange, cx + 126, cy + 22, 20, cx + 166, cy + 66, 72)
  })

  // Toolhead Carriage (Nextruder)
  const xCarriage = new THREE.Group()
  zGantry.add(xCarriage)

  let glowMesh: THREE.Mesh | null = null
  let nozzleLight: THREE.PointLight | null = null

  k.withGroup(xCarriage, () => {
    k.box(p.black, cx - 28, cy - 22, 8, cx + 28, cy + 30, 74)
    k.box(p.orange, cx - 22, cy - 40, 10, cx + 22, cy - 8, 46)
    k.box(p.gold, cx - 8, cy - 8, 3, cx + 8, cy + 8, 10)
    k.cyl(p.gold, 'z', cx, cy, 1.5, 3, 3)

    const glowGeo = new THREE.SphereGeometry(1.2, 8, 8)
    const glowMat = new THREE.MeshBasicMaterial({ color: 0x38bdf8, transparent: true, opacity: 0.9 })
    glowMesh = new THREE.Mesh(glowGeo, glowMat)
    glowMesh.position.set(cx, cy, 0.4)
    xCarriage.add(glowMesh)

    nozzleLight = new THREE.PointLight(0x38bdf8, 1.2, 35)
    nozzleLight.position.set(cx, cy, 1.0)
    xCarriage.add(nozzleLight)
  })

  let curX = cx
  let curY = cy
  let curZ = defaultNozzleZ

  const kinematics: PrinterKinematics = {
    setNozzlePosition(x: number, y: number, z: number, isExtruding = true) {
      curX = x
      curY = y
      curZ = z
      zGantry.position.z = z
      xCarriage.position.x = x - cx
      xCarriage.position.y = y - cy
      bedGroup.position.y = (cy - y) * 0.25
      if (nozzleLight) {
        nozzleLight.color.setHex(isExtruding ? 0x38bdf8 : 0xff7043)
        nozzleLight.intensity = isExtruding ? 1.4 : 0.3
      }
      if (glowMesh) {
        ;(glowMesh.material as THREE.MeshBasicMaterial).color.setHex(isExtruding ? 0x38bdf8 : 0xff7043)
      }
    },
    home() {
      kinematics.setNozzlePosition(cx, cy, defaultNozzleZ, false)
    },
    getNozzlePosition() {
      return { x: curX, y: curY, z: curZ }
    },
  }

  kinematics.home()
  return kinematics
}

// ---------------------------------------------------------------------------
// Bambu Lab X1 Carbon / P1S - enclosed CoreXY, 389 x 389 x 457 mm body
// ---------------------------------------------------------------------------

function bambu(k: Kit, p: Palette, v: BuildVolumeMm, variant: 'x1c' | 'p1s'): PrinterKinematics {
  const { cx, cy } = k
  const hx = v.x / 2 + 66.5
  const hy = v.y / 2 + 66.5
  const zb = -72
  const zt = v.z + 129
  const x0 = cx - hx
  const x1 = cx + hx
  const y0 = cy - hy
  const y1 = cy + hy
  const defaultNozzleZ = v.z + 10
  const edge = 0x0a0a0a

  // Plinth and corner posts
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

  // Panels
  k.box(p.sidePanel, x0, y0 + 16, zb + 14, x0 + 3, y1 - 16, zt - 14, edge)
  k.box(p.sidePanel, x1 - 3, y0 + 16, zb + 14, x1, y1 - 16, zt - 14, edge)
  k.box(p.sidePanel, x0 + 16, y1 - 3, zb + 14, x1 - 16, y1, zt - 14, edge)
  const door = variant === 'x1c' ? p.glassSmoke : p.glassClear
  const lid = variant === 'x1c' ? p.glassSmoke : p.lidPlastic
  k.box(door, x0 + 16, y0, zb + 44, x1 - 16, y0 + 3, zt - 14, edge)
  k.box(lid, x0 + 16, y0 + 16, zt - 4, x1 - 16, y1 - 16, zt, edge)
  k.box(p.frame, x0 + 16, y0, zb + 14, x1 - 16, y0 + 4, zb + 44)
  k.box(p.black, x0 + 24, y0 - 5, zb + 120, x0 + 29, y0 + 1, zb + 180)
  k.box(p.screen, x1 - 100, y0 - 2, zb + 20, x1 - 40, y0, zb + 40)

  // CoreXY upper Y rails (static)
  const railX = v.x / 2 + 22
  for (const side of [-1, 1]) {
    k.cyl(p.steel, 'y', cx + side * railX, cy, defaultNozzleZ + 38, 4, v.y + 40)
  }

  // CoreXY moving Gantry (moves along Y)
  const yGantry = new THREE.Group()
  k.group.add(yGantry)
  k.withGroup(yGantry, () => {
    k.cyl(p.darkGray, 'x', cx, cy, defaultNozzleZ + 42, 5, railX * 2)
  })

  // Toolhead Carriage (moves along X within yGantry, and moves in Z)
  const toolheadGroup = new THREE.Group()
  yGantry.add(toolheadGroup)

  let glowMesh: THREE.Mesh | null = null
  let nozzleLight: THREE.PointLight | null = null

  k.withGroup(toolheadGroup, () => {
    k.box(p.frame, cx - 28, cy - 30, 4, cx + 28, cy + 30, 64)
    k.box(p.black, cx - 20, cy - 36, 10, cx + 20, cy - 28, 40)
    k.box(p.gold, cx - 7, cy - 7, 3, cx + 7, cy + 7, 9)
    k.cyl(p.gold, 'z', cx, cy, 1.5, 3, 3)

    const glowGeo = new THREE.SphereGeometry(1.2, 8, 8)
    const glowMat = new THREE.MeshBasicMaterial({ color: 0x38bdf8, transparent: true, opacity: 0.9 })
    glowMesh = new THREE.Mesh(glowGeo, glowMat)
    glowMesh.position.set(cx, cy, 0.4)
    toolheadGroup.add(glowMesh)

    nozzleLight = new THREE.PointLight(0x38bdf8, 1.2, 35)
    nozzleLight.position.set(cx, cy, 1.0)
    toolheadGroup.add(nozzleLight)
  })

  // Bed (CoreXY drops bed down in Z)
  const bedGroup = new THREE.Group()
  k.group.add(bedGroup)
  k.withGroup(bedGroup, () => {
    bed(k, p, v.x + 1, v.y + 1, p.peiGold, 8)
    k.box(p.frame, cx - 110, cy, -32, cx + 110, y1 - 6, -18)
    k.box(p.darkGray, cx - 100, cy - v.y / 2, -18, cx + 100, cy + v.y / 2, -10.5)
    for (const side of [-1, 1]) {
      k.cyl(p.steel, 'z', cx + side * 110, y1 - 14, (zb + 14 + zt - 20) / 2, 4, zt - 20 - zb - 14)
    }
  })

  let curX = cx
  let curY = cy
  let curZ = defaultNozzleZ

  const kinematics: PrinterKinematics = {
    setNozzlePosition(x: number, y: number, z: number, isExtruding = true) {
      curX = x
      curY = y
      curZ = z
      toolheadGroup.position.x = x - cx
      yGantry.position.y = y - cy
      toolheadGroup.position.z = z
      if (nozzleLight) {
        nozzleLight.color.setHex(isExtruding ? 0x38bdf8 : 0xff7043)
        nozzleLight.intensity = isExtruding ? 1.4 : 0.3
      }
      if (glowMesh) {
        ;(glowMesh.material as THREE.MeshBasicMaterial).color.setHex(isExtruding ? 0x38bdf8 : 0xff7043)
      }
    },
    home() {
      kinematics.setNozzlePosition(cx, cy, defaultNozzleZ, false)
    },
    getNozzlePosition() {
      return { x: curX, y: curY, z: curZ }
    },
  }

  kinematics.home()
  return kinematics
}

// ---------------------------------------------------------------------------
// Voron 2.4 (350) - enclosed CoreXY cube, extrusion frame, red toolhead
// ---------------------------------------------------------------------------

function voron24(k: Kit, p: Palette, v: BuildVolumeMm): PrinterKinematics {
  const { cx, cy } = k
  const hx = v.x / 2 + 75
  const hy = v.y / 2 + 85
  const zb = -85
  const zt = v.z + 100
  const x0 = cx - hx
  const x1 = cx + hx
  const y0 = cy - hy
  const y1 = cy + hy
  const defaultNozzleZ = v.z + 10
  const edge = 0x4a5a66

  // 2020 frame
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
  k.box(p.black, x0 + 20, y0 + 20, zb, x1 - 20, y1 - 20, zb + 8)

  // Panels
  k.box(p.voronPanel, x0, y0 + 20, zb + 20, x0 + 3, y1 - 20, zt - 20, edge)
  k.box(p.voronPanel, x1 - 3, y0 + 20, zb + 20, x1, y1 - 20, zt - 20, edge)
  k.box(p.voronPanel, x0 + 20, y1 - 3, zb + 20, x1 - 20, y1, zt - 20, edge)
  k.box(p.voronPanel, x0 + 20, y0, zb + 20, cx - 1.5, y0 + 3, zt - 20, edge)
  k.box(p.voronPanel, cx + 1.5, y0, zb + 20, x1 - 20, y0 + 3, zt - 20, edge)
  k.box(p.voronPanel, x0 + 20, y0 + 20, zt - 3, x1 - 20, y1 - 20, zt, edge)
  k.box(p.black, cx - 14, y0 - 8, zb + 150, cx - 8, y0, zb + 330)
  k.box(p.black, cx + 8, y0 - 8, zb + 150, cx + 14, y0, zb + 330)

  // Four Z lead screws/belts
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

  // Static bed
  k.box(p.frame, cx - v.x / 2, cy - v.y / 2, -30, cx + v.x / 2, cy + v.y / 2, -12)
  bed(k, p, v.x, v.y, p.pei, 9.5)

  // Flying Z Gantry (moves in Z)
  const zGantry = new THREE.Group()
  k.group.add(zGantry)

  const railX = v.x / 2 + 38
  k.withGroup(zGantry, () => {
    for (const side of [-1, 1]) {
      k.box(p.frame, cx + side * railX - 10, y0 + 30, 45, cx + side * railX + 10, y1 - 30, 65)
    }
  })

  // Y crossbeam (moves in Y within zGantry)
  const yBeam = new THREE.Group()
  zGantry.add(yBeam)
  k.withGroup(yBeam, () => {
    k.box(p.frame, cx - railX - 10, cy + 12, 38, cx + railX + 10, cy + 36, 58)
  })

  // Stealthburner Toolhead (moves in X within yBeam)
  const xToolhead = new THREE.Group()
  yBeam.add(xToolhead)

  let glowMesh: THREE.Mesh | null = null
  let nozzleLight: THREE.PointLight | null = null

  k.withGroup(xToolhead, () => {
    k.box(p.black, cx - 26, cy - 28, 6, cx + 26, cy + 34, 64)
    k.box(p.red, cx - 20, cy - 38, 22, cx + 20, cy - 12, 56)
    k.box(p.gold, cx - 7, cy - 7, 3, cx + 7, cy + 7, 9)
    k.cyl(p.gold, 'z', cx, cy, 1.5, 3, 3)

    const glowGeo = new THREE.SphereGeometry(1.2, 8, 8)
    const glowMat = new THREE.MeshBasicMaterial({ color: 0x38bdf8, transparent: true, opacity: 0.9 })
    glowMesh = new THREE.Mesh(glowGeo, glowMat)
    glowMesh.position.set(cx, cy, 0.4)
    xToolhead.add(glowMesh)

    nozzleLight = new THREE.PointLight(0x38bdf8, 1.2, 35)
    nozzleLight.position.set(cx, cy, 1.0)
    xToolhead.add(nozzleLight)
  })

  let curX = cx
  let curY = cy
  let curZ = defaultNozzleZ

  const kinematics: PrinterKinematics = {
    setNozzlePosition(x: number, y: number, z: number, isExtruding = true) {
      curX = x
      curY = y
      curZ = z
      zGantry.position.z = z
      yBeam.position.y = y - cy
      xToolhead.position.x = x - cx
      if (nozzleLight) {
        nozzleLight.color.setHex(isExtruding ? 0x38bdf8 : 0xff7043)
        nozzleLight.intensity = isExtruding ? 1.4 : 0.3
      }
      if (glowMesh) {
        ;(glowMesh.material as THREE.MeshBasicMaterial).color.setHex(isExtruding ? 0x38bdf8 : 0xff7043)
      }
    },
    home() {
      kinematics.setNozzlePosition(cx, cy, defaultNozzleZ, false)
    },
    getNozzlePosition() {
      return { x: curX, y: curY, z: curZ }
    },
  }

  kinematics.home()
  return kinematics
}

// ---------------------------------------------------------------------------

export function buildPrinterModel(kind: PrinterKind, volume: BuildVolumeMm): PrinterModel {
  const kit = new Kit(volume.x / 2, volume.y / 2)
  const pal = palette()
  let kinematics: PrinterKinematics | undefined

  switch (kind) {
    case 'ender3':
      kinematics = ender3(kit, pal, volume)
      break
    case 'mk4s':
      kinematics = mk4s(kit, pal, volume)
      break
    case 'bambu-x1c':
      kinematics = bambu(kit, pal, volume, 'x1c')
      break
    case 'bambu-p1s':
      kinematics = bambu(kit, pal, volume, 'p1s')
      break
    case 'voron-2-4':
      kinematics = voron24(kit, pal, volume)
      break
  }

  const bounds = new THREE.Box3().setFromObject(kit.group)
  return { kind, title: TITLES[kind], group: kit.group, bounds, kinematics }
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
