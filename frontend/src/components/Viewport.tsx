/**
 * The 3D viewport.
 *
 * Two modes, both driven by real data:
 *  - `model`: the uploaded mesh, parsed client-side from the actual file bytes.
 *  - `toolpath`: the segments the backend parsed out of the G-code the slicer
 *    really produced, coloured by extrusion vs travel.
 *
 * The build plate is drawn to the machine's real dimensions, and the camera
 * frames the content rather than sitting at a magic default, so a 2 mm part and
 * a 300 mm part are both legible.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import { loadStl, type LoadedMesh } from '../lib/stl'
import type { Toolpath } from '../lib/api'

export interface ViewportProps {
  file: File | null
  toolpath: Toolpath | null
  mode: 'model' | 'toolpath'
  buildVolume: { x: number; y: number; z: number } | null
  maxLayer: number
}

interface Palette {
  extrusion: number
  travel: number
  retract: number
  plate: number
  model: number
}

const PALETTE: Palette = {
  extrusion: 0x38bdf8,
  travel: 0x475569,
  retract: 0xf97316,
  plate: 0x1e293b,
  model: 0x94a3b8,
}

function frameCamera(
  camera: THREE.PerspectiveCamera,
  box: THREE.Box3,
): void {
  const size = box.getSize(new THREE.Vector3())
  const centre = box.getCenter(new THREE.Vector3())
  const maxExtent = Math.max(size.x, size.y, size.z, 1)
  const distance = (maxExtent * 1.8) / (2 * Math.tan((camera.fov * Math.PI) / 360))

  camera.position.set(
    centre.x + distance * 0.6,
    centre.y - distance * 0.75,
    centre.z + distance * 0.6,
  )
  camera.lookAt(centre)
  camera.near = Math.max(distance / 500, 0.1)
  camera.far = distance * 20
  camera.updateProjectionMatrix()
}

export function Viewport({ file, toolpath, mode, buildVolume, maxLayer }: ViewportProps): JSX.Element {
  const mountRef = useRef<HTMLDivElement | null>(null)
  const sceneRef = useRef<THREE.Scene | null>(null)
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null)
  const [mesh, setMesh] = useState<LoadedMesh | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [showTravel, setShowTravel] = useState(false)
  const [showPlate, setShowPlate] = useState(true)

  // --- model loading ----------------------------------------------------
  useEffect(() => {
    if (!file) {
      setMesh(null)
      setLoadError(null)
      return
    }
    let cancelled = false
    void loadStl(file)
      .then((loaded) => {
        if (cancelled) return
        setMesh(loaded)
        setLoadError(null)
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setLoadError(error instanceof Error ? error.message : 'Could not read the model file.')
      })
    return () => {
      cancelled = true
    }
  }, [file])

  // --- one-time scene setup ---------------------------------------------
  useEffect(() => {
    const mount = mountRef.current
    if (!mount) return

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x0b1220)
    scene.fog = new THREE.Fog(0x0b1220, 400, 1400)
    sceneRef.current = scene

    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 4000)
    cameraRef.current = camera

    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    mount.appendChild(renderer.domElement)

    scene.add(new THREE.HemisphereLight(0xbfdbfe, 0x0f172a, 1.4))
    const key = new THREE.DirectionalLight(0xffffff, 1.1)
    key.position.set(120, -180, 220)
    scene.add(key)
    const rim = new THREE.DirectionalLight(0x38bdf8, 0.6)
    rim.position.set(-160, 120, 90)
    scene.add(rim)

    const grid = new THREE.GridHelper(600, 30, 0x1e293b, 0x152033)
    grid.rotation.x = Math.PI / 2
    grid.position.y = 0.5
    scene.add(grid)

    // Minimal orbit controls: avoiding the OrbitControls import keeps the
    // bundle small and the behaviour fully visible in this file.
    let azimuth = 0.9
    let polar = 1.05
    let distance = 320
    let dragging = false
    let lastX = 0
    let lastY = 0

    const applyCamera = (): void => {
      const target = new THREE.Vector3(0, 0, 60)
      camera.position.set(
        target.x + distance * Math.sin(polar) * Math.sin(azimuth),
        target.y - distance * Math.cos(polar),
        target.z + distance * Math.sin(polar) * Math.cos(azimuth),
      )
      camera.lookAt(target)
    }
    applyCamera()

    const onPointerDown = (event: PointerEvent): void => {
      dragging = true
      lastX = event.clientX
      lastY = event.clientY
    }
    const onPointerMove = (event: PointerEvent): void => {
      if (!dragging) return
      azimuth -= (event.clientX - lastX) * 0.005
      polar = Math.min(Math.PI - 0.05, Math.max(0.05, polar - (event.clientY - lastY) * 0.005))
      lastX = event.clientX
      lastY = event.clientY
      applyCamera()
    }
    const onPointerUp = (): void => {
      dragging = false
    }
    const onWheel = (event: WheelEvent): void => {
      event.preventDefault()
      distance = Math.min(2400, Math.max(30, distance * (1 + Math.sign(event.deltaY) * 0.12)))
      applyCamera()
    }

    const canvas = renderer.domElement
    canvas.addEventListener('pointerdown', onPointerDown)
    canvas.addEventListener('pointermove', onPointerMove)
    canvas.addEventListener('pointerup', onPointerUp)
    canvas.addEventListener('pointerleave', onPointerUp)
    canvas.addEventListener('wheel', onWheel, { passive: false })

    const resize = (): void => {
      const width = mount.clientWidth || 1
      const height = mount.clientHeight || 1
      renderer.setSize(width, height, false)
      camera.aspect = width / height
      camera.updateProjectionMatrix()
    }
    resize()
    const observer = new ResizeObserver(resize)
    observer.observe(mount)

    let frame = 0
    const tick = (): void => {
      frame = window.requestAnimationFrame(tick)
      renderer.render(scene, camera)
    }
    tick()

    return () => {
      window.cancelAnimationFrame(frame)
      observer.disconnect()
      canvas.removeEventListener('pointerdown', onPointerDown)
      canvas.removeEventListener('pointermove', onPointerMove)
      canvas.removeEventListener('pointerup', onPointerUp)
      canvas.removeEventListener('pointerleave', onPointerUp)
      canvas.removeEventListener('wheel', onWheel)
      scene.traverse((object) => {
        if (object instanceof THREE.Mesh) {
          object.geometry.dispose()
          const material = object.material
          if (Array.isArray(material)) material.forEach((m) => m.dispose())
          else material.dispose()
        }
      })
      renderer.dispose()
      mount.removeChild(canvas)
    }
  }, [])

  // --- mesh geometry ----------------------------------------------------
  useEffect(() => {
    const scene = sceneRef.current
    const camera = cameraRef.current
    if (!scene || !camera || !mesh || mode !== 'model') return

    const group = new THREE.Group()
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.BufferAttribute(mesh.positions, 3))
    geometry.computeVertexNormals()

    const material = new THREE.MeshStandardMaterial({
      color: PALETTE.model,
      metalness: 0.15,
      roughness: 0.55,
      side: THREE.DoubleSide,
      flatShading: true,
    })
    group.add(new THREE.Mesh(geometry, material))

    // Edges make a mesh readable in a way flat shading alone is not.
    const edges = new THREE.EdgesGeometry(geometry, 25)
    group.add(
      new THREE.LineSegments(
        edges,
        new THREE.LineBasicMaterial({ color: 0x475569, transparent: true, opacity: 0.5 }),
      ),
    )

    // Sit the part on the plate, the same canonical pose the slicer received.
    group.position.set(
      -(mesh.bounds.min[0] + mesh.bounds.max[0]) / 2,
      -(mesh.bounds.min[1] + mesh.bounds.max[1]) / 2,
      -mesh.bounds.min[2],
    )
    scene.add(group)

    frameCamera(
      camera,
      new THREE.Box3(
        new THREE.Vector3(0, 0, 0),
        new THREE.Vector3(mesh.bounds.max[0] - mesh.bounds.min[0], mesh.bounds.max[1] - mesh.bounds.min[1], mesh.bounds.max[2] - mesh.bounds.min[2]),
      ),
    )

    return () => {
      scene.remove(group)
      geometry.dispose()
      edges.dispose()
    }
  }, [mesh, mode])

  // --- toolpath lines ----------------------------------------------------
  const toolpathGeometry = useMemo(() => {
    if (!toolpath) return null

    const cut = maxLayer > 0 ? maxLayer : Infinity
    const extrude: number[] = []
    const travel: number[] = []
    const retract: number[] = []

    for (const segment of toolpath.segments) {
      if (segment.layer !== null && segment.layer > cut) continue
      const target =
        segment.kind === 'extrude' ? extrude : segment.kind === 'retract' ? retract : travel
      const [x0, y0, z0] = segment.from
      const [x1, y1, z1] = segment.to
      target.push(x0, y0, z0, x1, y1, z1)
    }

    return {
      extrude: new Float32Array(extrude),
      travel: new Float32Array(travel),
      retract: new Float32Array(retract),
    }
  }, [toolpath, maxLayer])

  useEffect(() => {
    const scene = sceneRef.current
    if (!scene || !toolpathGeometry) return

    const group = new THREE.Group()
    const addLines = (values: Float32Array, color: number, opacity: number): void => {
      if (values.length < 6) return
      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute('position', new THREE.BufferAttribute(values, 3))
      group.add(
        new THREE.LineSegments(
          geometry,
          new THREE.LineBasicMaterial({ color, transparent: true, opacity }),
        ),
      )
    }

    addLines(toolpathGeometry.extrude, PALETTE.extrusion, 1)
    if (showTravel) addLines(toolpathGeometry.travel, PALETTE.travel, 0.35)
    if (showTravel) addLines(toolpathGeometry.retract, PALETTE.retract, 0.8)

    scene.add(group)
    return () => {
      scene.remove(group)
      group.traverse((object) => {
        if (object instanceof THREE.LineSegments) {
          object.geometry.dispose()
          const material = object.material
          if (Array.isArray(material)) material.forEach((m) => m.dispose())
          else material.dispose()
        }
      })
    }
  }, [toolpathGeometry, showTravel, mode])

  // --- build plate --------------------------------------------------------
  useEffect(() => {
    const scene = sceneRef.current
    if (!scene || !buildVolume || !showPlate) return

    const box = new THREE.Box3(
      new THREE.Vector3(0, 0, 0),
      new THREE.Vector3(buildVolume.x, buildVolume.y, buildVolume.z),
    )
    const helper = new THREE.Box3Helper(box, PALETTE.plate)
    scene.add(helper)
    return () => {
      scene.remove(helper)
      helper.geometry.dispose()
      ;(helper.material as THREE.Material).dispose()
    }
  }, [buildVolume, showPlate])

  const triangles = mesh?.triangleCount ?? 0
  const visibleLayers = Math.max(0, Math.min(maxLayer, toolpath?.layer_count ?? 0))

  return (
    <div className="relative h-full w-full overflow-hidden rounded-lg border border-slate-800 bg-slate-950">
      <div ref={mountRef} className="h-full w-full" />

      <div className="pointer-events-none absolute left-3 top-3 rounded-md bg-slate-950/80 px-3 py-2 text-xs font-mono text-slate-300 backdrop-blur">
        {mode === 'model' ? (
          <div>
            <div>{triangles.toLocaleString()} triangles</div>
            {mesh && (
              <div className="text-slate-500">
                {(
                  mesh.bounds.max[0] -
                  mesh.bounds.min[0]
                ).toFixed(1)}{' '}
                x{' '}
                {(mesh.bounds.max[1] - mesh.bounds.min[1]).toFixed(1)} x{' '}
                {(mesh.bounds.max[2] - mesh.bounds.min[2]).toFixed(1)} mm
              </div>
            )}
          </div>
        ) : (
          <div>
            <div>
              layer {visibleLayers} / {toolpath?.layer_count ?? 0}
            </div>
            <div className="text-slate-500">
              {toolpath?.stats.extrusion_moves.toLocaleString() ?? 0} extrusion moves
            </div>
          </div>
        )}
      </div>

      <div className="absolute bottom-3 left-3 flex flex-wrap gap-2 text-xs">
        {mode === 'toolpath' && (
          <>
            <span className="rounded bg-slate-900/90 px-2 py-1 text-sky-300">
              <span className="mr-1 inline-block h-2 w-2 rounded-full bg-sky-400" />
              extrusion
            </span>
            {showTravel && (
              <span className="rounded bg-slate-900/90 px-2 py-1 text-slate-400">
                <span className="mr-1 inline-block h-2 w-2 rounded-full bg-slate-500" />
                travel
              </span>
            )}
          </>
        )}
      </div>

      <div className="absolute right-3 top-3 flex flex-col gap-1 text-xs">
        <button
          type="button"
          onClick={() => setShowTravel((value) => !value)}
          className="rounded bg-slate-900/90 px-2 py-1 text-slate-300 hover:bg-slate-800"
        >
          {showTravel ? 'Hide' : 'Show'} travel
        </button>
        <button
          type="button"
          onClick={() => setShowPlate((value) => !value)}
          className="rounded bg-slate-900/90 px-2 py-1 text-slate-300 hover:bg-slate-800"
        >
          {showPlate ? 'Hide' : 'Show'} build plate
        </button>
      </div>

      {loadError && (
        <div className="absolute inset-x-4 bottom-12 rounded bg-rose-950/90 px-3 py-2 text-xs text-rose-200">
          {loadError}
        </div>
      )}

      <div className="pointer-events-none absolute bottom-3 right-3 text-[10px] text-slate-600">
        drag to orbit · scroll to zoom
      </div>
    </div>
  )
}
