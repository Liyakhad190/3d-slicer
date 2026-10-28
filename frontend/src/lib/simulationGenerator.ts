import type { Toolpath, ToolpathSegment } from './api'
import type { LoadedMesh } from './stl'

export interface BoxBounds {
  min: [number, number, number]
  max: [number, number, number]
}

/**
 * Generates an authentic multi-layer Toolpath object for a 3D bounding box
 * sitting on the build plate. Used when simulating un-sliced models or demo models.
 */
export function generateSyntheticToolpath(
  bounds: BoxBounds,
  buildVolume: { x: number; y: number; z: number } | null,
  layerHeight = 0.25,
): Toolpath {
  const plateX = buildVolume?.x ?? 220
  const plateY = buildVolume?.y ?? 220
  const plateZ = buildVolume?.z ?? 250

  const width = Math.max(8, bounds.max[0] - bounds.min[0])
  const depth = Math.max(8, bounds.max[1] - bounds.min[1])
  const height = Math.max(2, bounds.max[2] - bounds.min[2])

  // Center on bed
  const cx = plateX / 2
  const cy = plateY / 2
  const x0 = cx - width / 2
  const x1 = cx + width / 2
  const y0 = cy - depth / 2
  const y1 = cy + depth / 2

  const layerCount = Math.max(8, Math.min(100, Math.round(height / layerHeight)))
  const segments: ToolpathSegment[] = []
  const layerZ: number[] = []

  let extrusionMoves = 0
  let travelMoves = 0
  let retractions = 0
  let filamentExtrudedMm = 0
  let lastPos: [number, number, number] = [0, 0, 10]

  // Initial travel to origin / start position
  segments.push({
    from: [0, 0, 10],
    to: [x0 - 5, y0 - 5, layerHeight],
    kind: 'travel',
    layer: 1,
    e: 0,
  })
  travelMoves += 1
  lastPos = [x0 - 5, y0 - 5, layerHeight]

  for (let l = 1; l <= layerCount; l++) {
    const z = Number((l * layerHeight).toFixed(3))
    layerZ.push(z)

    // Skirt or brim on layer 1
    if (l === 1) {
      const skirtMargin = 3
      const sx0 = x0 - skirtMargin
      const sx1 = x1 + skirtMargin
      const sy0 = y0 - skirtMargin
      const sy1 = y1 + skirtMargin

      segments.push({
        from: lastPos,
        to: [sx0, sy0, z],
        kind: 'travel',
        layer: 1,
        e: 0,
      })
      travelMoves += 1

      const skirtCorners: Array<[number, number, number]> = [
        [sx1, sy0, z],
        [sx1, sy1, z],
        [sx0, sy1, z],
        [sx0, sy0, z],
      ]
      let pFrom: [number, number, number] = [sx0, sy0, z]
      for (const pTo of skirtCorners) {
        const dist = Math.hypot(pTo[0] - pFrom[0], pTo[1] - pFrom[1])
        const eVal = dist * 0.05
        segments.push({ from: pFrom, to: pTo, kind: 'extrude', layer: 1, e: eVal })
        extrusionMoves += 1
        filamentExtrudedMm += eVal
        pFrom = pTo
      }
      lastPos = pFrom
    }

    // Travel to start of layer outer wall
    segments.push({
      from: lastPos,
      to: [x0, y0, z],
      kind: 'travel',
      layer: l,
      e: 0,
    })
    travelMoves += 1

    // 1. Outer perimeter
    const outerCorners: Array<[number, number, number]> = [
      [x1, y0, z],
      [x1, y1, z],
      [x0, y1, z],
      [x0, y0, z],
    ]
    let curFrom: [number, number, number] = [x0, y0, z]
    for (const curTo of outerCorners) {
      const dist = Math.hypot(curTo[0] - curFrom[0], curTo[1] - curFrom[1])
      const eVal = dist * 0.05
      segments.push({ from: curFrom, to: curTo, kind: 'extrude', layer: l, e: eVal })
      extrusionMoves += 1
      filamentExtrudedMm += eVal
      curFrom = curTo
    }

    // 2. Inner perimeter (inset by 0.45 mm)
    const inset = 0.45
    const ix0 = x0 + inset
    const ix1 = x1 - inset
    const iy0 = y0 + inset
    const iy1 = y1 - inset

    segments.push({
      from: curFrom,
      to: [ix0, iy0, z],
      kind: 'travel',
      layer: l,
      e: 0,
    })
    travelMoves += 1

    const innerCorners: Array<[number, number, number]> = [
      [ix1, iy0, z],
      [ix1, iy1, z],
      [ix0, iy1, z],
      [ix0, iy0, z],
    ]
    curFrom = [ix0, iy0, z]
    for (const curTo of innerCorners) {
      const dist = Math.hypot(curTo[0] - curFrom[0], curTo[1] - curFrom[1])
      const eVal = dist * 0.05
      segments.push({ from: curFrom, to: curTo, kind: 'extrude', layer: l, e: eVal })
      extrusionMoves += 1
      filamentExtrudedMm += eVal
      curFrom = curTo
    }

    // 3. Infill (alternating 45 deg or zig-zag patterns)
    const isSolid = l <= 2 || l >= layerCount - 2
    const infillStep = isSolid ? 0.45 : 3.0
    const margin = 1.0

    if (l % 2 === 0) {
      for (let y = iy0 + margin; y < iy1 - margin; y += infillStep) {
        segments.push({
          from: curFrom,
          to: [ix0 + margin, y, z],
          kind: 'travel',
          layer: l,
          e: 0,
        })
        travelMoves += 1

        const dist = ix1 - ix0 - 2 * margin
        const eVal = dist * 0.04
        segments.push({
          from: [ix0 + margin, y, z],
          to: [ix1 - margin, y, z],
          kind: 'extrude',
          layer: l,
          e: eVal,
        })
        extrusionMoves += 1
        filamentExtrudedMm += eVal
        curFrom = [ix1 - margin, y, z]
      }
    } else {
      for (let x = ix0 + margin; x < ix1 - margin; x += infillStep) {
        segments.push({
          from: curFrom,
          to: [x, iy0 + margin, z],
          kind: 'travel',
          layer: l,
          e: 0,
        })
        travelMoves += 1

        const dist = iy1 - iy0 - 2 * margin
        const eVal = dist * 0.04
        segments.push({
          from: [x, iy0 + margin, z],
          to: [x, iy1 - margin, z],
          kind: 'extrude',
          layer: l,
          e: eVal,
        })
        extrusionMoves += 1
        filamentExtrudedMm += eVal
        curFrom = [x, iy1 - margin, z]
      }
    }

    // Retraction & Z-hop
    segments.push({
      from: curFrom,
      to: [curFrom[0], curFrom[1], z + 0.3],
      kind: 'retract',
      layer: l,
      e: -1.0,
    })
    retractions += 1
    lastPos = [curFrom[0], curFrom[1], z + 0.3]
  }

  // Final park move
  segments.push({
    from: lastPos,
    to: [10, plateY - 10, height + 15],
    kind: 'travel',
    layer: layerCount,
    e: 0,
  })
  travelMoves += 1

  return {
    job_id: 'sim_synthetic',
    blocked: false,
    version: '1.0.0',
    layer_count: layerCount,
    layer_z: layerZ,
    bounds: {
      min: [x0, y0, 0],
      max: [x1, y1, height],
    },
    build_volume: buildVolume ?? { x: plateX, y: plateY, z: plateZ },
    stats: {
      travel_moves: travelMoves,
      extrusion_moves: extrusionMoves,
      retractions,
      filament_extruded_mm: Math.round(filamentExtrudedMm),
      build_volume: buildVolume ?? { x: plateX, y: plateY, z: plateZ },
    },
    segments,
    segments_elided: 0,
  }
}

/**
 * Builds a 20 mm calibration cube mesh for instant real-time simulation
 * right on the build plate.
 */
export function generateDemoCubeMesh(size = 20): LoadedMesh {
  const hs = size / 2
  const positions = new Float32Array([
    // Front (+Y)
    -hs, hs, 0,   hs, hs, 0,   hs, hs, size,
    -hs, hs, 0,   hs, hs, size, -hs, hs, size,
    // Back (-Y)
    hs, -hs, 0,  -hs, -hs, 0,  -hs, -hs, size,
    hs, -hs, 0,  -hs, -hs, size, hs, -hs, size,
    // Top (+Z)
    -hs, -hs, size, hs, -hs, size, hs, hs, size,
    -hs, -hs, size, hs, hs, size, -hs, hs, size,
    // Bottom (-Z)
    -hs, hs, 0,   hs, hs, 0,   hs, -hs, 0,
    -hs, hs, 0,   hs, -hs, 0, -hs, -hs, 0,
    // Right (+X)
    hs, -hs, 0,   hs, hs, 0,   hs, hs, size,
    hs, -hs, 0,   hs, hs, size, hs, -hs, size,
    // Left (-X)
    -hs, hs, 0,  -hs, -hs, 0,  -hs, -hs, size,
    -hs, hs, 0,  -hs, -hs, size, -hs, hs, size,
  ])

  return {
    positions,
    triangleCount: 12,
    bounds: {
      min: [-hs, -hs, 0],
      max: [hs, hs, size],
    },
  }
}
