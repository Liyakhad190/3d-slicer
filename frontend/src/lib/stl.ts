/**
 * STL/mesh loader for the viewer.
 *
 * Parses binary and ASCII STL in the browser. Deliberately hand-written rather
 * than pulling in a loader library: STL is a trivial format, and the viewer
 * must not depend on the backend for a triangle mesh.
 */

export interface LoadedMesh {
  positions: Float32Array
  normals?: Float32Array
  triangleCount: number
  bounds: { min: [number, number, number]; max: [number, number, number] }
}

const BINARY_STL_MAGIC = 80

function detectBinary(data: DataView): boolean {
  if (data.byteLength < 84) return false
  const faces = data.getUint32(BINARY_STL_MAGIC, true)
  if (faces === 0 || faces > 10_000_000) return false
  return data.byteLength === 84 + faces * 50
}

function parseBinary(data: DataView): LoadedMesh {
  const faceCount = data.getUint32(BINARY_STL_MAGIC, true)
  const positions = new Float32Array(faceCount * 9)
  let offset = 84

  for (let face = 0; face < faceCount; face += 1) {
    // 12-byte normal, then three vertices of 3 floats each.
    offset += 12
    for (let vertex = 0; vertex < 9; vertex += 1) {
      positions[face * 9 + vertex] = data.getFloat32(offset, true)
      offset += 4
    }
    offset += 2 // attribute byte count
  }

  return finalise(positions, faceCount)
}

function parseAscii(text: string): LoadedMesh {
  const values: number[] = []
  let faceCount = 0

  for (const rawLine of text.split('\n')) {
    const line = rawLine.trim()
    if (!line.startsWith('vertex')) continue
    const parts = line.split(/\s+/)
    if (parts.length < 4) continue
    values.push(Number(parts[1]), Number(parts[2]), Number(parts[3]))
    if (values.length % 9 === 0) faceCount += 1
  }

  return finalise(new Float32Array(values), faceCount)
}

function finalise(positions: Float32Array, triangleCount: number): LoadedMesh {
  const min: [number, number, number] = [Infinity, Infinity, Infinity]
  const max: [number, number, number] = [-Infinity, -Infinity, -Infinity]

  for (let i = 0; i < positions.length; i += 3) {
    for (let axis = 0; axis < 3; axis += 1) {
      const value = positions[i + axis]
      if (value === undefined) continue
      const lo = min[axis] ?? Infinity
      const hi = max[axis] ?? -Infinity
      if (value < lo) min[axis] = value
      if (value > hi) max[axis] = value
    }
  }

  if (!Number.isFinite(min[0])) {
    return {
      positions,
      triangleCount: 0,
      bounds: { min: [0, 0, 0], max: [0, 0, 0] },
    }
  }

  return { positions, triangleCount, bounds: { min, max } }
}

export async function loadStl(file: Blob): Promise<LoadedMesh> {
  const buffer = await file.arrayBuffer()
  const view = new DataView(buffer)
  if (detectBinary(view)) return parseBinary(view)
  return parseAscii(new TextDecoder().decode(buffer))
}
