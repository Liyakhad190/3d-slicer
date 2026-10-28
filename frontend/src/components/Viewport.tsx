/**
 * The 3D viewport with Real-Time Slicing & Printing Simulation.
 *
 * Modes:
 *  - `model`: the uploaded mesh, parsed client-side from the actual file bytes.
 *  - `toolpath`: the segments the backend parsed out of the G-code.
 *  - `simulation`: real-time machine printing simulation with physical toolhead/gantry
 *    kinematics, dynamic filament extrusion, holographic laser slicing, and telemetry HUD.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { loadStl, type LoadedMesh } from '../lib/stl'
import type { Toolpath, ToolpathSegment } from '../lib/api'
import {
  buildPrinterModel,
  disposePrinterModel,
  identifyPrinter,
  type PrinterModel,
} from '../lib/printerModels'
import { generateDemoCubeMesh, generateSyntheticToolpath } from '../lib/simulationGenerator'

export interface ViewportProps {
  file: File | null
  toolpath: Toolpath | null
  mode: 'model' | 'toolpath' | 'simulation'
  buildVolume: { x: number; y: number; z: number } | null
  printerLabel?: string | null
  motionSystem?: string | null
  maxLayer: number
  onFitToPrinter?: () => void
  isTinyModel?: boolean
  isOversizedModel?: boolean
  onModeChange?: (mode: 'model' | 'toolpath' | 'simulation') => void
  onLoadDemoModel?: () => void
}

interface Palette {
  extrusion: number
  travel: number
  retract: number
  plate: number
  model: number
}

type CameraView = 'isometric' | 'top' | 'bottom' | 'front' | 'back' | 'left' | 'right'
type SimVisualMode = 'print' | 'laser_slice'

const PALETTE: Palette = {
  extrusion: 0x1a73e8,
  travel: 0x90caf9,
  retract: 0xff7043,
  plate: 0xc8c8c8,
  model: 0x2f6fdb,
}

function formatMm(value: number): string {
  if (!Number.isFinite(value)) return '—'
  if (value === 0) return '0.0'
  const magnitude = Math.abs(value)
  if (magnitude >= 100) return value.toFixed(1)
  if (magnitude >= 10) return value.toFixed(2)
  if (magnitude >= 1) return value.toFixed(3)
  return Number(value.toPrecision(3)).toString()
}

function formatTime(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds))
  const mins = Math.floor(s / 60)
  const secs = s % 60
  const hours = Math.floor(mins / 60)
  if (hours > 0) {
    return `${hours}h ${mins % 60}m`
  }
  return `${mins}:${secs.toString().padStart(2, '0')}`
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
  const distance =
    Math.max(
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
  rail.quaternion.setFromUnitVectors(new THREE.Vector3(1, 0, 0), direction.normalize())
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
  onModeChange,
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
  const [isDemoModel, setIsDemoModel] = useState(false)

  // --- Real-time Simulation State ---------------------------------------
  const [simActive, setSimActive] = useState(mode === 'simulation')
  const [simPlaying, setSimPlaying] = useState(false)
  const [simProgress, setSimProgress] = useState(0) // 0 to 1
  const [simSpeed, setSimSpeed] = useState<number>(5) // 1x, 2x, 5x, 10x, 25x, 50x
  const [simVisualMode, setSimVisualMode] = useState<SimVisualMode>('print')
  const [simLayer, setSimLayer] = useState<number>(1)
  const [simCoords, setSimCoords] = useState<{ x: number; y: number; z: number }>({ x: 0, y: 0, z: 0 })
  const [simFeedrate, setSimFeedrate] = useState<number>(0)
  const [simAction, setSimAction] = useState<string>('Ready to simulate')
  const [simExtruding, setSimExtruding] = useState<boolean>(false)

  buildVolumeRef.current = buildVolume

  useEffect(() => {
    if (mode === 'simulation') {
      setSimActive(true)
      setSimPlaying(true)
    } else {
      setSimActive(false)
      setSimPlaying(false)
    }
  }, [mode])

  // Printer 3D model
  const printerKind = useMemo(() => identifyPrinter(printerLabel), [printerLabel])
  const buildVolumeKey = buildVolume ? `${buildVolume.x}:${buildVolume.y}:${buildVolume.z}` : ''

  const printerModel = useMemo<PrinterModel | null>(
    () => (printerKind && buildVolume ? buildPrinterModel(printerKind, buildVolume) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [printerKind, buildVolumeKey],
  )
  printerBoundsRef.current = printerModel ? printerModel.bounds : null

  // Procedural frame fallback kinematics reference
  const fallbackKinematicsRef = useRef<{
    setNozzlePosition: (x: number, y: number, z: number, isExtruding?: boolean) => void
  } | null>(null)

  // Load uploaded STL
  useEffect(() => {
    if (!file) {
      if (!isDemoModel) {
        setMesh(null)
      }
      setLoadError(null)
      return
    }
    setIsDemoModel(false)
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
  }, [file, isDemoModel])

  // Active toolpath resolution (real or synthetic)
  const activeToolpath = useMemo<Toolpath | null>(() => {
    if (toolpath && toolpath.segments.length > 0) return toolpath
    if (mesh && buildVolume) {
      return generateSyntheticToolpath(mesh.bounds, buildVolume)
    }
    return null
  }, [toolpath, mesh, buildVolume])

  // Start demo model and simulation immediately
  const handleLoadDemoAndSimulate = useCallback(() => {
    const demo = generateDemoCubeMesh(20)
    setMesh(demo)
    setIsDemoModel(true)
    setSimActive(true)
    setSimPlaying(true)
    setSimProgress(0)
    if (onModeChange) onModeChange('simulation')
  }, [onModeChange])

  // --- one-time scene setup ---------------------------------------------
  useEffect(() => {
    const mount = mountRef.current
    if (!mount) return

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0xffffff)
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
      if (mount.contains(canvas)) mount.removeChild(canvas)
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

  // --- Mesh geometry in model or laser_slice mode -----------------------
  useEffect(() => {
    const scene = sceneRef.current
    const camera = cameraRef.current
    if (!scene || !camera || !mesh || (mode !== 'model' && mode !== 'simulation')) return
    const controls = controlsRef.current
    if (!controls) return

    const group = new THREE.Group()
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.BufferAttribute(mesh.positions, 3))
    geometry.computeVertexNormals()

    const isGhost = mode === 'simulation'
    const material = new THREE.MeshStandardMaterial({
      color: PALETTE.model,
      metalness: 0.15,
      roughness: 0.55,
      side: THREE.DoubleSide,
      flatShading: true,
      transparent: isGhost,
      opacity: isGhost ? 0.35 : 1.0,
      depthWrite: !isGhost,
    })
    group.add(new THREE.Mesh(geometry, material))

    const edges = new THREE.EdgesGeometry(geometry, 25)
    group.add(
      new THREE.LineSegments(
        edges,
        new THREE.LineBasicMaterial({
          color: isGhost ? 0x64748b : 0x8a8a8a,
          transparent: true,
          opacity: isGhost ? 0.25 : 0.5,
        }),
      ),
    )

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
    group.renderOrder = isGhost ? 1 : 10
    scene.add(group)

    return () => {
      scene.remove(group)
      geometry.dispose()
      edges.dispose()
    }
  }, [mesh, mode, buildVolumeKey])

  // --- Static toolpath in toolpath mode ---------------------------------
  const toolpathGeometry = useMemo(() => {
    if (!toolpath || mode !== 'toolpath') return null

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
  }, [toolpath, maxLayer, mode])

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

  // --- Build plate ------------------------------------------------------
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
    const grid = new THREE.LineSegments(gridGeometry, new THREE.LineBasicMaterial({ color: 0xe0e0e0 }))
    grid.position.z = 0.005
    plateGroup.add(grid)

    const borderGeometry = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(0, 0, 0),
      new THREE.Vector3(buildVolume.x, 0, 0),
      new THREE.Vector3(buildVolume.x, buildVolume.y, 0),
      new THREE.Vector3(0, buildVolume.y, 0),
    ])
    const border = new THREE.LineLoop(borderGeometry, new THREE.LineBasicMaterial({ color: 0x8a8a8a }))
    border.position.z = 0.1
    plateGroup.add(border)

    scene.add(plateGroup)
    return () => {
      scene.remove(plateGroup)
      plateGroup.traverse((object) => {
        if (
          object instanceof THREE.Mesh ||
          object instanceof THREE.LineSegments ||
          object instanceof THREE.LineLoop
        ) {
          object.geometry.dispose()
          const material = object.material
          if (Array.isArray(material)) material.forEach((item) => item.dispose())
          else material.dispose()
        }
      })
    }
  }, [buildVolumeKey, showPlate])

  // --- Printer 3D model -------------------------------------------------
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

  // Procedural frame fallback
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

    const rearLeft = new THREE.Vector3(x0, y1, z0)
    const rearRight = new THREE.Vector3(x1, y1, z0)
    const topLeft = new THREE.Vector3(x0, y1, z1)
    const topRight = new THREE.Vector3(x1, y1, z1)
    addRail(frame, rearLeft, topLeft, railSize, material)
    addRail(frame, rearRight, topRight, railSize, material)
    addRail(frame, rearLeft, rearRight, railSize, material)
    addRail(frame, new THREE.Vector3(x0, y0, z0), new THREE.Vector3(x1, y0, z0), railSize, material)
    addRail(frame, new THREE.Vector3(x0, y0, z0), rearLeft, railSize, material)
    addRail(frame, new THREE.Vector3(x1, y0, z0), rearRight, railSize, material)
    addRail(frame, topLeft, topRight, railSize, material)

    // Moving carriage and nozzle for fallback frame
    const carriageGroup = new THREE.Group()
    const carriageSize = Math.max(12, railSize * 3)
    const carriageMesh = new THREE.Mesh(
      new THREE.BoxGeometry(carriageSize, carriageSize * 0.8, carriageSize),
      material,
    )
    carriageMesh.position.set(0, 0, carriageSize * 0.4)
    carriageGroup.add(carriageMesh)

    const nozzleTip = new THREE.Mesh(
      new THREE.CylinderGeometry(1.5, 3, 4, 12),
      new THREE.MeshBasicMaterial({ color: 0xddaa33 }),
    )
    nozzleTip.position.set(0, 0, 2)
    carriageGroup.add(nozzleTip)

    const nozzleLight = new THREE.PointLight(0x38bdf8, 1.2, 30)
    nozzleLight.position.set(0, 0, 1)
    carriageGroup.add(nozzleLight)

    carriageGroup.position.set(buildVolume.x / 2, buildVolume.y / 2, buildVolume.z * 0.7)
    frame.add(carriageGroup)

    fallbackKinematicsRef.current = {
      setNozzlePosition(x: number, y: number, z: number, isExtruding = true) {
        carriageGroup.position.set(x, y, z)
        nozzleLight.color.setHex(isExtruding ? 0x38bdf8 : 0xff7043)
        nozzleLight.intensity = isExtruding ? 1.4 : 0.3
      },
    }

    scene.add(frame)
    return () => {
      scene.remove(frame)
      fallbackKinematicsRef.current = null
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

  // =====================================================================
  // REAL-TIME SIMULATION ENGINE
  // =====================================================================
  const simSegmentsRef = useRef<ToolpathSegment[]>([])
  const simLineMeshRef = useRef<THREE.LineSegments | null>(null)
  const simLaserGroupRef = useRef<THREE.Group | null>(null)

  // Pre-load simulation line buffer
  useEffect(() => {
    const scene = sceneRef.current
    if (!scene || !activeToolpath || (mode !== 'simulation' && !simActive)) {
      if (simLineMeshRef.current) {
        scene?.remove(simLineMeshRef.current)
        simLineMeshRef.current.geometry.dispose()
        simLineMeshRef.current = null
      }
      return
    }

    const segments = activeToolpath.segments
    simSegmentsRef.current = segments

    const totalSegs = segments.length
    const positions = new Float32Array(totalSegs * 6)
    const colors = new Float32Array(totalSegs * 6)

    for (let i = 0; i < totalSegs; i++) {
      const seg = segments[i]!
      const [x0, y0, z0] = seg.from
      const [x1, y1, z1] = seg.to
      const idx = i * 6

      positions[idx] = x0
      positions[idx + 1] = y0
      positions[idx + 2] = z0
      positions[idx + 3] = x1
      positions[idx + 4] = y1
      positions[idx + 5] = z1

      const c =
        seg.kind === 'extrude'
          ? new THREE.Color(0x38bdf8)
          : seg.kind === 'retract'
            ? new THREE.Color(0xff7043)
            : new THREE.Color(0x94a3b8)

      colors[idx] = c.r
      colors[idx + 1] = c.g
      colors[idx + 2] = c.b
      colors[idx + 3] = c.r
      colors[idx + 4] = c.g
      colors[idx + 5] = c.b
    }

    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
    geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3))
    geometry.setDrawRange(0, 0)

    const material = new THREE.LineBasicMaterial({
      vertexColors: true,
      transparent: true,
      opacity: 0.95,
      linewidth: 2,
    })

    const lineMesh = new THREE.LineSegments(geometry, material)
    lineMesh.renderOrder = 5
    scene.add(lineMesh)
    simLineMeshRef.current = lineMesh

    // Holographic Laser Slicing Plane group
    const laserGroup = new THREE.Group()
    if (buildVolume) {
      const laserPlane = new THREE.Mesh(
        new THREE.PlaneGeometry(buildVolume.x + 10, buildVolume.y + 10),
        new THREE.MeshBasicMaterial({
          color: 0x06b6d4,
          transparent: true,
          opacity: 0.2,
          side: THREE.DoubleSide,
          depthWrite: false,
        }),
      )
      laserPlane.position.set(buildVolume.x / 2, buildVolume.y / 2, 0)
      laserGroup.add(laserPlane)

      const laserRing = new THREE.LineLoop(
        new THREE.BufferGeometry().setFromPoints([
          new THREE.Vector3(-5, -5, 0),
          new THREE.Vector3(buildVolume.x + 5, -5, 0),
          new THREE.Vector3(buildVolume.x + 5, buildVolume.y + 5, 0),
          new THREE.Vector3(-5, buildVolume.y + 5, 0),
        ]),
        new THREE.LineBasicMaterial({ color: 0x22d3ee }),
      )
      laserGroup.add(laserRing)
    }
    laserGroup.visible = simVisualMode === 'laser_slice'
    scene.add(laserGroup)
    simLaserGroupRef.current = laserGroup

    return () => {
      scene.remove(lineMesh)
      geometry.dispose()
      material.dispose()
      simLineMeshRef.current = null

      scene.remove(laserGroup)
      laserGroup.traverse((obj) => {
        if (obj instanceof THREE.Mesh || obj instanceof THREE.LineLoop) {
          obj.geometry.dispose()
        }
      })
      simLaserGroupRef.current = null
    }
  }, [activeToolpath, mode, simActive, buildVolume, simVisualMode])

  // Simulation Animation Loop
  const simProgressRef = useRef(simProgress)
  simProgressRef.current = simProgress
  const simSpeedRef = useRef(simSpeed)
  simSpeedRef.current = simSpeed
  const simPlayingRef = useRef(simPlaying)
  simPlayingRef.current = simPlaying

  useEffect(() => {
    if (!simActive && mode !== 'simulation') return

    let lastTime = performance.now()
    let animId: number

    const simLoop = (now: number): void => {
      animId = requestAnimationFrame(simLoop)
      const dt = Math.min((now - lastTime) / 1000, 0.1)
      lastTime = now

      const segments = simSegmentsRef.current
      if (segments.length === 0) return

      let progress = simProgressRef.current

      if (simPlayingRef.current) {
        // Step forward proportional to simulation speed
        const progressIncrement = (0.015 * simSpeedRef.current * dt) / Math.max(1, segments.length / 200)
        progress = Math.min(1.0, progress + progressIncrement)
        if (progress >= 1.0) {
          setSimPlaying(false)
        }
        setSimProgress(progress)
      }

      // Map progress to active segment
      const currentSegmentIndex = Math.min(
        segments.length - 1,
        Math.floor(progress * segments.length),
      )
      const seg = segments[currentSegmentIndex]

      if (seg) {
        const segFrac = (progress * segments.length) - currentSegmentIndex
        const curX = seg.from[0] + (seg.to[0] - seg.from[0]) * segFrac
        const curY = seg.from[1] + (seg.to[1] - seg.from[1]) * segFrac
        const curZ = seg.from[2] + (seg.to[2] - seg.from[2]) * segFrac
        const isExtr = seg.kind === 'extrude'

        // Apply kinematics to printer model
        if (printerModel?.kinematics) {
          printerModel.kinematics.setNozzlePosition(curX, curY, curZ, isExtr)
        } else if (fallbackKinematicsRef.current) {
          fallbackKinematicsRef.current.setNozzlePosition(curX, curY, curZ, isExtr)
        }

        // Update Laser Slicing plane position
        if (simLaserGroupRef.current) {
          simLaserGroupRef.current.position.z = curZ
          simLaserGroupRef.current.visible = simVisualMode === 'laser_slice'
        }

        // Update dynamic drawn lines
        if (simLineMeshRef.current) {
          simLineMeshRef.current.geometry.setDrawRange(0, (currentSegmentIndex + 1) * 2)
        }

        // Telemetry updates
        const feedrate = isExtr ? 60 : seg.kind === 'retract' ? 40 : 150
        setSimCoords({ x: Number(curX.toFixed(2)), y: Number(curY.toFixed(2)), z: Number(curZ.toFixed(2)) })
        setSimLayer(seg.layer ?? 1)
        setSimFeedrate(feedrate)
        setSimExtruding(isExtr)
        setSimAction(
          isExtr
            ? `Extruding Layer ${seg.layer ?? 1}`
            : seg.kind === 'retract'
              ? 'Retracting & Z-Hop'
              : 'Rapid Travel Move',
        )
      }
    }

    animId = requestAnimationFrame(simLoop)
    return () => {
      cancelAnimationFrame(animId)
    }
  }, [simActive, mode, printerModel, simVisualMode])

  const totalSimLayers = activeToolpath?.layer_count ?? 67
  const triangles = mesh?.triangleCount ?? 0
  const visibleLayers = Math.max(0, Math.min(maxLayer, toolpath?.layer_count ?? 0))

  return (
    <div className="relative h-full w-full overflow-hidden rounded-lg border border-slate-800 bg-slate-950 select-none">
      <div ref={mountRef} className="h-full w-full" />

      {/* Info Badge (Top Left) */}
      <div className="pointer-events-none absolute left-3 top-3 rounded-md bg-slate-950/85 px-3 py-2 text-xs font-mono text-slate-300 backdrop-blur shadow-md border border-slate-800/80">
        <div className="font-semibold text-sky-400 flex items-center gap-1.5">
          <span>{printerLabel || 'Creality Ender 3'}</span>
          <span className="text-[10px] text-slate-400 font-normal">
            · {printerKind ? 'Kinematic 3D Machine' : 'Profile Frame'}
          </span>
        </div>

        {mode === 'simulation' || simActive ? (
          <div className="mt-1 space-y-0.5 text-[11px]">
            <div className="flex items-center gap-2">
              <span className="flex h-2 w-2 relative">
                <span
                  className={[
                    'animate-ping absolute inline-flex h-full w-full rounded-full opacity-75',
                    simPlaying ? 'bg-emerald-400' : 'bg-amber-400',
                  ].join(' ')}
                />
                <span
                  className={[
                    'relative inline-flex rounded-full h-2 w-2',
                    simPlaying ? 'bg-emerald-500' : 'bg-amber-500',
                  ].join(' ')}
                />
              </span>
              <span className="font-semibold text-slate-200">
                {simPlaying ? 'LIVE SIMULATION' : 'SIMULATION PAUSED'}
              </span>
              <span className="text-slate-400">({(simProgress * 100).toFixed(0)}%)</span>
            </div>
            <div className="text-sky-300 font-medium">{simAction}</div>
          </div>
        ) : mode === 'model' ? (
          <div>
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

      {/* Prominent Demo Simulation CTA Banner when no model is loaded */}
      {!mesh && mode !== 'simulation' && (
        <div className="absolute inset-x-4 top-16 mx-auto flex max-w-md flex-col items-center gap-3 rounded-xl border border-sky-500/40 bg-slate-900/90 p-4 text-center shadow-2xl backdrop-blur">
          <div className="text-sm font-semibold text-sky-200">
            Real-Time Slicing & Machine Simulation
          </div>
          <p className="text-xs text-slate-400">
            Watch the {printerLabel || 'Creality Ender 3'} extruder, gantry, and bed move in 3D
            while slicing and depositing filament in real-time.
          </p>
          <button
            type="button"
            onClick={handleLoadDemoAndSimulate}
            className="flex items-center gap-2 rounded-lg bg-sky-600 px-4 py-2 text-xs font-semibold text-white shadow-lg shadow-sky-600/30 hover:bg-sky-500 transition active:scale-95"
          >
            <span>⚡</span>
            <span>Load 20mm Demo Cube & Start Real-Time Simulation</span>
          </button>
        </div>
      )}

      {/* Real-time Telemetry HUD (Top Center/Right) */}
      {(mode === 'simulation' || simActive) && (
        <div className="absolute left-1/2 -translate-x-1/2 top-3 flex items-center gap-4 rounded-lg border border-slate-800 bg-slate-950/90 px-4 py-1.5 text-[11px] font-mono text-slate-300 shadow-xl backdrop-blur">
          <div className="flex items-center gap-1.5">
            <span className="text-slate-500">POS:</span>
            <span className="text-sky-300 font-bold">
              X{simCoords.x} Y{simCoords.y} Z{simCoords.z}
            </span>
          </div>
          <div className="h-3 w-px bg-slate-800" />
          <div className="flex items-center gap-1.5">
            <span className="text-slate-500">LAYER:</span>
            <span className="text-emerald-400 font-bold">
              {simLayer} / {totalSimLayers}
            </span>
          </div>
          <div className="h-3 w-px bg-slate-800" />
          <div className="flex items-center gap-1.5">
            <span className="text-slate-500">SPEED:</span>
            <span className="text-amber-300">{simFeedrate} mm/s</span>
          </div>
          <div className="h-3 w-px bg-slate-800" />
          <div className="flex items-center gap-1.5">
            <span className={simExtruding ? 'text-sky-400 font-bold' : 'text-slate-400'}>
              {simExtruding ? '● EXTRUDING' : '○ TRAVEL'}
            </span>
          </div>
          <div className="h-3 w-px bg-slate-800" />
          <div className="flex items-center gap-1.5">
            <span className="text-rose-400">🔥 215°C</span>
            <span className="text-amber-400">🛏️ 60°C</span>
          </div>
        </div>
      )}

      {/* Real-Time Simulation Player Control Bar (Bottom Center) */}
      {(mode === 'simulation' || simActive) && (
        <div className="absolute bottom-4 left-1/2 -translate-x-1/2 flex w-[94%] max-w-2xl flex-col gap-2 rounded-xl border border-sky-500/30 bg-slate-950/95 p-3 shadow-2xl backdrop-blur">
          {/* Scrubber slider */}
          <div className="flex items-center gap-3">
            <span className="text-[10px] font-mono text-slate-400">
              {formatTime(simProgress * ((activeToolpath?.stats.filament_extruded_mm ?? 100) * 1.5 + 120))}
            </span>
            <input
              type="range"
              min={0}
              max={1}
              step={0.001}
              value={simProgress}
              onChange={(e) => {
                setSimProgress(Number(e.target.value))
              }}
              className="flex-1 accent-sky-400 h-1.5 bg-slate-800 rounded-lg cursor-pointer"
            />
            <span className="text-[10px] font-mono text-slate-500">
              {formatTime((activeToolpath?.stats.filament_extruded_mm ?? 100) * 1.5 + 120)}
            </span>
          </div>

          {/* Controls toolbar */}
          <div className="flex items-center justify-between text-xs">
            {/* Play/Pause & Reset */}
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setSimPlaying((p) => !p)}
                className="flex h-8 items-center gap-1.5 rounded-lg bg-sky-600 px-3 font-semibold text-white shadow hover:bg-sky-500 transition active:scale-95"
              >
                {simPlaying ? <span>⏸ Pause</span> : <span>▶ Play</span>}
              </button>
              <button
                type="button"
                onClick={() => {
                  setSimProgress(0)
                  setSimPlaying(false)
                  if (printerModel?.kinematics) printerModel.kinematics.home()
                }}
                className="flex h-8 items-center rounded-lg border border-slate-800 bg-slate-900 px-2.5 text-slate-300 hover:bg-slate-800"
                title="Restart simulation"
              >
                ⏮ Restart
              </button>
            </div>

            {/* Visual Mode Toggle */}
            <div className="flex rounded-lg border border-slate-800 bg-slate-900 p-0.5">
              <button
                type="button"
                onClick={() => setSimVisualMode('print')}
                className={[
                  'rounded px-2.5 py-1 text-[11px] font-medium transition',
                  simVisualMode === 'print' ? 'bg-sky-600 text-white' : 'text-slate-400 hover:text-white',
                ].join(' ')}
              >
                🖨️ Machine Print Motion
              </button>
              <button
                type="button"
                onClick={() => setSimVisualMode('laser_slice')}
                className={[
                  'rounded px-2.5 py-1 text-[11px] font-medium transition',
                  simVisualMode === 'laser_slice'
                    ? 'bg-cyan-600 text-white'
                    : 'text-slate-400 hover:text-white',
                ].join(' ')}
              >
                📐 Holographic Slicing Laser
              </button>
            </div>

            {/* Speed Multipliers */}
            <div className="flex items-center gap-1">
              <span className="text-[10px] text-slate-500 mr-1">Speed:</span>
              {[1, 2, 5, 10, 25].map((spd) => (
                <button
                  key={spd}
                  type="button"
                  onClick={() => setSimSpeed(spd)}
                  className={[
                    'rounded px-1.5 py-0.5 text-[10px] font-mono font-medium transition',
                    simSpeed === spd
                      ? 'bg-sky-500 text-white'
                      : 'bg-slate-900 text-slate-400 hover:bg-slate-800',
                  ].join(' ')}
                >
                  {spd}×
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Viewport Control Panel (Top Right) */}
      <div className="absolute right-3 top-3 flex flex-col gap-1 text-xs">
        {/* Toggle Real-Time Simulation button */}
        <button
          type="button"
          onClick={() => {
            if (simActive) {
              setSimActive(false)
              setSimPlaying(false)
              if (onModeChange) onModeChange('model')
            } else {
              setSimActive(true)
              setSimPlaying(true)
              if (onModeChange) onModeChange('simulation')
            }
          }}
          className={[
            'rounded px-2.5 py-1.5 font-semibold transition flex items-center justify-center gap-1.5 shadow',
            simActive || mode === 'simulation'
              ? 'bg-emerald-600 text-white hover:bg-emerald-500'
              : 'bg-gradient-to-r from-sky-600 to-indigo-600 text-white hover:opacity-90',
          ].join(' ')}
        >
          <span>⚡</span>
          <span>{simActive ? 'Exit Simulation' : 'Live Simulation'}</span>
        </button>

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
