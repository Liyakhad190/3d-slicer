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
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { loadStl, type LoadedMesh } from '../lib/stl'
import type { Toolpath } from '../lib/api'
import {
  buildPrinterModel,
  disposePrinterModel,
  identifyPrinter,
  type PrinterModel,
} from '../lib/printerModels'

export interface ViewportProps {
  file: File | null
  toolpath: Toolpath | null
  mode: 'model' | 'toolpath'
  buildVolume: { x: number; y: number; z: number } | null
  printerLabel?: string | null
  motionSystem?: string | null
  maxLayer: number
  onFitToPrinter?: () => void
  isTinyModel?: boolean
  isOversizedModel?: boolean
}

interface Palette {
  extrusion: number
  travel: number
  retract: number
  plate: number
  model: number
}

type CameraView = 'isometric' | 'top' | 'bottom' | 'front' | 'back' | 'left' | 'right'

const PALETTE: Palette = {
  extrusion: 0x1a73e8,
  travel: 0x90caf9,
  retract: 0xff7043,
  plate: 0xc8c8c8,
  model: 0x2f6fdb,
}

/**
 * Format an edge length in millimetres at a precision that survives small parts.
 *
 * A fixed `toFixed(1)` reports a 0.02 mm model (one exported in meters) as
 * "0.0", which reads as an empty scene rather than a real, if tiny, mesh. The
 * step is chosen so three significant figures are always shown.
 */
function formatMm(value: number): string {
  if (!Number.isFinite(value)) return '—'
  if (value === 0) return '0.0'
  const magnitude = Math.abs(value)
  if (magnitude >= 100) return value.toFixed(1)
  if (magnitude >= 10) return value.toFixed(2)
  if (magnitude >= 1) return value.toFixed(3)
  // Sub-millimetre: significant figures, never a flat zero.
  return Number(value.toPrecision(3)).toString()
}

function frameCamera(
  camera: THREE.PerspectiveCamera,
  controls: OrbitControls,
  box: THREE.Box3,
  padding = 1.45,
): void {
  const sphere = box.getBoundingSphere(new THREE.Sphere())
  const verticalFov = THREE.MathUtils.degToRad(camera.fov)
  const horizontalFov = 2 * Math.atan(Math.tan(verticalFov / 2) * Math.max(camera.aspect, 0.1))
  const distance = Math.max(
    sphere.radius / Math.sin(verticalFov / 2),
    sphere.radius / Math.sin(horizontalFov / 2),
  ) * padding
  const direction = new THREE.Vector3(1, -1.1, 1.15).normalize()

  controls.target.copy(sphere.center)
  camera.position.copy(sphere.center).addScaledVector(direction, distance)
  camera.near = Math.max(distance / 2000, 0.1)
  camera.far = Math.max(distance * 12, 5000)
  controls.minDistance = 2
  controls.maxDistance = Math.max(distance * 6, 3000)
  camera.updateProjectionMatrix()
  controls.update()
}

function addRail(
  group: THREE.Group,
  start: THREE.Vector3,
  end: THREE.Vector3,
  thickness: number,
  material: THREE.Material,
): void {
  const direction = end.clone().sub(start)
  const rail = new THREE.Mesh(
    new THREE.BoxGeometry(direction.length(), thickness, thickness),
    material,
  )
  rail.position.copy(start).add(end).multiplyScalar(0.5)
  rail.quaternion.setFromUnitVectors(
    new THREE.Vector3(1, 0, 0),
    direction.normalize(),
  )
  group.add(rail)
}

export function Viewport({
  file,
  toolpath,
  mode,
  buildVolume,
  printerLabel,
  motionSystem,
  maxLayer,
  onFitToPrinter,
}: ViewportProps): JSX.Element {
  const mountRef = useRef<HTMLDivElement | null>(null)
  const sceneRef = useRef<THREE.Scene | null>(null)
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null)
  const controlsRef = useRef<OrbitControls | null>(null)
  const buildVolumeRef = useRef(buildVolume)
  const homeViewRef = useRef<() => void>(() => undefined)
  const printerBoundsRef = useRef<THREE.Box3 | null>(null)
  const [mesh, setMesh] = useState<LoadedMesh | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [showTravel, setShowTravel] = useState(false)
  const [showPlate, setShowPlate] = useState(true)
  const [showPrinterFrame, setShowPrinterFrame] = useState(true)
  const [cameraView, setCameraView] = useState<CameraView>('isometric')
  const [displayScale, setDisplayScale] = useState(1)
  buildVolumeRef.current = buildVolume

  // The exact printer that was selected, if we have a model for it.
  const printerKind = useMemo(() => identifyPrinter(printerLabel), [printerLabel])

  const buildVolumeKey = buildVolume
    ? `${buildVolume.x}:${buildVolume.y}:${buildVolume.z}`
    : ''

  const printerModel = useMemo<PrinterModel | null>(
    () => (printerKind && buildVolume ? buildPrinterModel(printerKind, buildVolume) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [printerKind, buildVolumeKey],
  )
  printerBoundsRef.current = printerModel ? printerModel.bounds : null

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
    scene.background = new THREE.Color(0xffffff)
    // No fog: with the camera framed on the whole printer, fog faded the part to white.
    sceneRef.current = scene

    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 4000)
    camera.up.set(0, 0, 1)
    cameraRef.current = camera

    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    mount.appendChild(renderer.domElement)

    scene.add(new THREE.HemisphereLight(0xffffff, 0xdddddd, 1.4))
    const key = new THREE.DirectionalLight(0xffffff, 1.1)
    key.position.set(120, -180, 220)
    scene.add(key)
    const rim = new THREE.DirectionalLight(0xffffff, 0.6)
    rim.position.set(-160, 120, 90)
    scene.add(rim)

    const canvas = renderer.domElement
    canvas.style.touchAction = 'none'
    const controls = new OrbitControls(camera, canvas)
    controls.enableDamping = true
    controls.dampingFactor = 0.08
    controls.enablePan = true
    controls.screenSpacePanning = true
    controls.zoomSpeed = 0.8
    controlsRef.current = controls
    homeViewRef.current = () => {
      const volume = buildVolumeRef.current
      if (volume) {
        // Frame the build volume & bed area so the bed and model are front-and-center, like real slicers
        const margin = Math.max(volume.x, volume.y) * 0.08
        const framingBox = new THREE.Box3(
          new THREE.Vector3(-margin, -margin, -10),
          new THREE.Vector3(
            volume.x + margin,
            volume.y + margin,
            Math.min(volume.z * 0.85, Math.max(volume.x, volume.y) * 0.9),
          ),
        )
        frameCamera(camera, controls, framingBox, 1.4)
        setCameraView('isometric')
        return
      }
      const printerBounds = printerBoundsRef.current
      if (printerBounds) {
        frameCamera(camera, controls, printerBounds, 1.25)
        setCameraView('isometric')
        return
      }
      const framingBox = new THREE.Box3(new THREE.Vector3(-110, -110, 0), new THREE.Vector3(110, 110, 0))
      frameCamera(camera, controls, framingBox, 1.9)
      setCameraView('isometric')
    }

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
      controls.update()
      renderer.render(scene, camera)
    }
    tick()

    return () => {
      window.cancelAnimationFrame(frame)
      observer.disconnect()
      controls.dispose()
      controlsRef.current = null
      homeViewRef.current = () => undefined
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

  useEffect(() => {
    homeViewRef.current()
  }, [buildVolumeKey, printerKind])

  function selectCameraView(view: CameraView): void {
    setCameraView(view)
    if (view === 'isometric') {
      homeViewRef.current()
      return
    }

    const camera = cameraRef.current
    const controls = controlsRef.current
    if (!camera || !controls) return

    const direction = {
      top: new THREE.Vector3(0, 0, 1),
      bottom: new THREE.Vector3(0, 0, -1),
      front: new THREE.Vector3(0, -1, 0),
      back: new THREE.Vector3(0, 1, 0),
      left: new THREE.Vector3(-1, 0, 0),
      right: new THREE.Vector3(1, 0, 0),
    }[view]
    const target = controls.target.clone()
    const distance = camera.position.distanceTo(target)
    const verticalView = view === 'top' || view === 'bottom'
    camera.up.set(0, verticalView ? 1 : 0, verticalView ? 0 : 1)
    camera.position.copy(target).addScaledVector(direction, distance)
    camera.lookAt(target)
    camera.updateProjectionMatrix()
    controls.update()
  }

  // --- mesh geometry ----------------------------------------------------
  useEffect(() => {
    const scene = sceneRef.current
    const camera = cameraRef.current
    if (!scene || !camera || !mesh || mode !== 'model') return
    const controls = controlsRef.current
    if (!controls) return

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
        new THREE.LineBasicMaterial({ color: 0x8a8a8a, transparent: true, opacity: 0.5 }),
      ),
    )

    // Sit the part on the plate, inside the printer.
    // If the part is tiny (< 5 mm, e.g. exported in meters like 0.12 mm), scale it up for
    // visual preview so it fits comfortably on the bed (~65% bed size) and is never hidden!
    // If larger than the build volume, scale down to fit inside frame.
    const plate = buildVolumeRef.current
    const sizeX = mesh.bounds.max[0] - mesh.bounds.min[0]
    const sizeY = mesh.bounds.max[1] - mesh.bounds.min[1]
    const sizeZ = mesh.bounds.max[2] - mesh.bounds.min[2]
    const maxDim = Math.max(sizeX, sizeY, sizeZ)
    let fit = 1
    if (plate && maxDim > 0 && sizeX > 0 && sizeY > 0 && sizeZ > 0) {
      if (maxDim < 5) {
        fit = Math.min((plate.x * 0.65) / sizeX, (plate.y * 0.65) / sizeY, (plate.z * 0.65) / sizeZ)
      } else if (sizeX > plate.x || sizeY > plate.y || sizeZ > plate.z) {
        fit = Math.min((plate.x * 0.95) / sizeX, (plate.y * 0.95) / sizeY, (plate.z * 0.95) / sizeZ)
      }
    }
    group.scale.setScalar(fit)
    setDisplayScale(fit)
    const cx = (mesh.bounds.min[0] + mesh.bounds.max[0]) / 2
    const cy = (mesh.bounds.min[1] + mesh.bounds.max[1]) / 2
    group.position.set(
      (plate ? plate.x / 2 : 0) - cx * fit,
      (plate ? plate.y / 2 : 0) - cy * fit,
      -mesh.bounds.min[2] * fit,
    )
    group.renderOrder = 10
    group.traverse((object) => {
      object.renderOrder = 10
    })
    scene.add(group)
    if (plate) {
      homeViewRef.current()
    } else {
      const width = mesh.bounds.max[0] - mesh.bounds.min[0]
      const depth = mesh.bounds.max[1] - mesh.bounds.min[1]
      const height = mesh.bounds.max[2] - mesh.bounds.min[2]
      frameCamera(
        camera,
        controls,
        new THREE.Box3(
          new THREE.Vector3(-width / 2, -depth / 2, 0),
          new THREE.Vector3(width / 2, depth / 2, height),
        ),
      )
    }

    return () => {
      scene.remove(group)
      geometry.dispose()
      edges.dispose()
    }
  }, [mesh, mode, buildVolumeKey])

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
    if (!scene || !toolpathGeometry || mode !== 'toolpath') return

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

    // Frame camera on the toolpath when entering toolpath mode
    const camera = cameraRef.current
    const controls = controlsRef.current
    if (camera && controls && buildVolumeRef.current) {
      homeViewRef.current()
    }

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

    const plateGroup = new THREE.Group()
    const plate = new THREE.Mesh(
      new THREE.PlaneGeometry(buildVolume.x, buildVolume.y),
      new THREE.MeshBasicMaterial({
        color: 0xfafafa,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: 0.22,
        depthWrite: false,
      }),
    )
    plate.renderOrder = 0
    plate.position.set(buildVolume.x / 2, buildVolume.y / 2, -0.25)
    plateGroup.add(plate)

    const spacing = Math.max(5, Math.round(Math.max(buildVolume.x, buildVolume.y) / 20 / 5) * 5)
    const gridPositions: number[] = []
    for (let x = 0; x <= buildVolume.x; x += spacing) {
      gridPositions.push(x, 0, 0, x, buildVolume.y, 0)
    }
    for (let y = 0; y <= buildVolume.y; y += spacing) {
      gridPositions.push(0, y, 0, buildVolume.x, y, 0)
    }
    const gridGeometry = new THREE.BufferGeometry()
    gridGeometry.setAttribute('position', new THREE.Float32BufferAttribute(gridPositions, 3))
    const grid = new THREE.LineSegments(
      gridGeometry,
      new THREE.LineBasicMaterial({ color: 0xe0e0e0 }),
    )
    grid.position.z = 0.005
    plateGroup.add(grid)

    const borderGeometry = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(0, 0, 0),
      new THREE.Vector3(buildVolume.x, 0, 0),
      new THREE.Vector3(buildVolume.x, buildVolume.y, 0),
      new THREE.Vector3(0, buildVolume.y, 0),
    ])
    const border = new THREE.LineLoop(
      borderGeometry,
      new THREE.LineBasicMaterial({ color: 0x8a8a8a }),
    )
    border.position.z = 0.1
    plateGroup.add(border)

    scene.add(plateGroup)
    return () => {
      scene.remove(plateGroup)
      plateGroup.traverse((object) => {
        if (object instanceof THREE.Mesh || object instanceof THREE.LineSegments || object instanceof THREE.LineLoop) {
          object.geometry.dispose()
          const material = object.material
          if (Array.isArray(material)) material.forEach((item) => item.dispose())
          else material.dispose()
        }
      })
    }
  }, [buildVolumeKey, showPlate])

  // --- the selected printer's own 3D model ----------------------------------
  useEffect(() => {
    const scene = sceneRef.current
    if (!scene || !printerModel || !showPrinterFrame) return
    scene.add(printerModel.group)
    return () => {
      scene.remove(printerModel.group)
    }
  }, [printerModel, showPrinterFrame])

  useEffect(() => {
    return () => {
      if (printerModel) disposePrinterModel(printerModel)
    }
  }, [printerModel])

  useEffect(() => {
    const scene = sceneRef.current
    if (!scene || !buildVolume || !showPrinterFrame || printerKind) return

    const frame = new THREE.Group()
    const material = new THREE.MeshBasicMaterial({ color: 0x000000, fog: false })
    const extent = Math.max(buildVolume.x, buildVolume.y)
    const margin = Math.max(8, extent * 0.045)
    const railSize = Math.max(4, Math.min(7, extent * 0.024))
    const x0 = -margin
    const y0 = -margin
    const x1 = buildVolume.x + margin
    const y1 = buildVolume.y + margin
    const z0 = -railSize
    const z1 = buildVolume.z
    const system = motionSystem?.toLowerCase() ?? ''
    const triangular = system === 'delta'
    const enclosedFrame = system.includes('corexy') || system.includes('corexz')
    const gantryZ = z1 * 0.72
    if (triangular) {
      const posts = [
        new THREE.Vector3(buildVolume.x / 2, y1, z0),
        new THREE.Vector3(x0, y0, z0),
        new THREE.Vector3(x1, y0, z0),
      ]
      const topPosts = posts.map((post) => new THREE.Vector3(post.x, post.y, z1))
      for (let index = 0; index < posts.length; index += 1) {
        const next = (index + 1) % posts.length
        addRail(frame, posts[index]!, topPosts[index]!, railSize, material)
        addRail(frame, topPosts[index]!, topPosts[next]!, railSize, material)
      }
      const effector = new THREE.Mesh(
        new THREE.SphereGeometry(railSize * 2.2, 12, 8),
        material,
      )
      effector.position.set(buildVolume.x / 2, buildVolume.y / 2, gantryZ)
      frame.add(effector)
    } else if (enclosedFrame) {
      const posts = [
        new THREE.Vector3(x0, y0, z0),
        new THREE.Vector3(x1, y0, z0),
        new THREE.Vector3(x1, y1, z0),
        new THREE.Vector3(x0, y1, z0),
      ]
      const topPosts = posts.map((post) => new THREE.Vector3(post.x, post.y, z1))
      for (let index = 0; index < posts.length; index += 1) {
        const next = (index + 1) % posts.length
        addRail(frame, posts[index]!, topPosts[index]!, railSize, material)
        addRail(frame, posts[index]!, posts[next]!, railSize, material)
        addRail(frame, topPosts[index]!, topPosts[next]!, railSize, material)
      }
      addRail(
        frame,
        new THREE.Vector3(x0, y1, gantryZ),
        new THREE.Vector3(x1, y1, gantryZ),
        railSize * 1.25,
        material,
      )
      const carriage = new THREE.Mesh(
        new THREE.BoxGeometry(railSize * 3, railSize * 3, railSize * 4),
        material,
      )
      carriage.position.set(buildVolume.x / 2, y1, gantryZ - railSize * 2)
      frame.add(carriage)
    } else {
      const rearLeft = new THREE.Vector3(x0, y1, z0)
      const rearRight = new THREE.Vector3(x1, y1, z0)
      const topLeft = new THREE.Vector3(x0, y1, z1)
      const topRight = new THREE.Vector3(x1, y1, z1)
      const gantryLeft = new THREE.Vector3(x0, y1, gantryZ)
      const gantryRight = new THREE.Vector3(x1, y1, gantryZ)
      addRail(frame, rearLeft, topLeft, railSize, material)
      addRail(frame, rearRight, topRight, railSize, material)
      addRail(frame, rearLeft, rearRight, railSize, material)
      addRail(frame, new THREE.Vector3(x0, y0, z0), new THREE.Vector3(x1, y0, z0), railSize, material)
      addRail(frame, new THREE.Vector3(x0, y0, z0), rearLeft, railSize, material)
      addRail(frame, new THREE.Vector3(x1, y0, z0), rearRight, railSize, material)
      addRail(frame, topLeft, topRight, railSize, material)
      addRail(frame, gantryLeft, gantryRight, railSize * 1.25, material)
      addRail(frame, rearLeft, new THREE.Vector3(x0, y0, z0), railSize, material)
      addRail(frame, rearRight, new THREE.Vector3(x1, y0, z0), railSize, material)

      const carriageSize = Math.max(12, railSize * 3)
      const carriage = new THREE.Mesh(
        new THREE.BoxGeometry(carriageSize, carriageSize * 0.8, carriageSize),
        material,
      )
      carriage.position.set(buildVolume.x / 2, y1, gantryZ - carriageSize * 0.7)
      frame.add(carriage)
      addRail(
        frame,
        new THREE.Vector3(buildVolume.x / 2, y1, gantryZ - carriageSize * 1.2),
        new THREE.Vector3(buildVolume.x / 2, y1, gantryZ - carriageSize * 2.1),
        railSize * 0.6,
        material,
      )
    }

    scene.add(frame)
    return () => {
      scene.remove(frame)
      frame.traverse((object) => {
        if (object instanceof THREE.Mesh) {
          object.geometry.dispose()
          const meshMaterial = object.material
          if (Array.isArray(meshMaterial)) meshMaterial.forEach((item) => item.dispose())
          else meshMaterial.dispose()
        }
      })
      material.dispose()
    }
  }, [buildVolumeKey, motionSystem, showPrinterFrame, printerKind])

  const triangles = mesh?.triangleCount ?? 0
  const visibleLayers = Math.max(0, Math.min(maxLayer, toolpath?.layer_count ?? 0))

  return (
    <div className="relative h-full w-full overflow-hidden rounded-lg border border-slate-800 bg-slate-950">
      <div ref={mountRef} className="h-full w-full" />

      <div className="pointer-events-none absolute left-3 top-3 rounded-md bg-slate-950/80 px-3 py-2 text-xs font-mono text-slate-300 backdrop-blur">
        {mode === 'model' ? (
          <div>
            {printerLabel && (
              <div className="font-semibold">
                {printerLabel} · {printerKind ? '3D model' : 'profile schematic'}
              </div>
            )}
            <div>{mesh ? `${triangles.toLocaleString()} triangles` : 'No model loaded'}</div>
            {mesh && displayScale < 0.98 && (
              <div className="text-amber-400">
                larger than build volume · shown at {(displayScale * 100).toFixed(0)}%
              </div>
            )}
            {mesh && displayScale > 1.05 && (
              <div className="flex items-center gap-1.5 text-amber-300">
                <span>Auto-fitted to bed ({displayScale.toFixed(0)}× preview)</span>
                {onFitToPrinter && (
                  <button
                    type="button"
                    onClick={onFitToPrinter}
                    className="pointer-events-auto rounded bg-sky-600 px-1.5 py-0.5 text-[10px] font-sans font-medium text-white hover:bg-sky-500"
                  >
                    Save to model
                  </button>
                )}
              </div>
            )}
            {mesh && (
              <div className="text-slate-500">
                {formatMm(mesh.bounds.max[0] - mesh.bounds.min[0])} x{' '}
                {formatMm(mesh.bounds.max[1] - mesh.bounds.min[1])} x{' '}
                {formatMm(mesh.bounds.max[2] - mesh.bounds.min[2])} mm
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
        {onFitToPrinter && mode === 'model' && mesh && (
          <button
            type="button"
            onClick={onFitToPrinter}
            className="rounded bg-sky-600 px-2 py-1 text-xs font-semibold text-white shadow hover:bg-sky-500 transition"
            title="Fit model proportionally inside the printer build volume"
          >
            ✨ Fit to printer
          </button>
        )}
        <button
          type="button"
          onClick={() => setShowTravel((value) => !value)}
          className="rounded bg-slate-900/90 px-2 py-1 text-slate-300 hover:bg-slate-800"
        >
          {showTravel ? 'Hide' : 'Show'} travel
        </button>
        <label className="flex items-center justify-between gap-2 rounded bg-slate-900/90 px-2 py-1 text-slate-300">
          <span>View</span>
          <select
            className="camera-view-select"
            value={cameraView}
            onChange={(event) => selectCameraView(event.target.value as CameraView)}
            aria-label="Camera view"
          >
            <option value="isometric">Isometric</option>
            <option value="top">Top</option>
            <option value="bottom">Bottom</option>
            <option value="front">Front</option>
            <option value="back">Back</option>
            <option value="left">Left</option>
            <option value="right">Right</option>
          </select>
        </label>
        <button
          type="button"
          onClick={() => homeViewRef.current()}
          className="rounded bg-slate-900/90 px-2 py-1 text-slate-300 hover:bg-slate-800"
          title="Reset camera to fit the full build plate"
          aria-label="Reset camera view"
        >
          Reset view
        </button>
        <button
          type="button"
          onClick={() => setShowPlate((value) => !value)}
          className="rounded bg-slate-900/90 px-2 py-1 text-slate-300 hover:bg-slate-800"
        >
          {showPlate ? 'Hide' : 'Show'} build plate
        </button>
        <button
          type="button"
          onClick={() => setShowPrinterFrame((value) => !value)}
          className="rounded bg-slate-900/90 px-2 py-1 text-slate-300 hover:bg-slate-800"
          title="Show or hide the selected printer"
        >
          {showPrinterFrame ? 'Hide' : 'Show'} printer
        </button>
      </div>

      {loadError && (
        <div className="absolute inset-x-4 bottom-12 rounded bg-rose-950/90 px-3 py-2 text-xs text-rose-200">
          {loadError}
        </div>
      )}

      <div className="pointer-events-none absolute bottom-3 right-3 text-[10px] text-slate-600">
        drag to orbit · right-drag to pan · scroll to zoom
      </div>
    </div>
  )
}
