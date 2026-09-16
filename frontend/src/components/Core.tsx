import { useEffect, useRef } from 'react'
import { useStore } from '../state/store'
import type { AssistantState } from '../lib/events'

/**
 * The central visualisation.
 *
 * One canvas, 2D context, a handful of arcs and a particle ring — deliberately
 * cheap to draw so it can run continuously without warming the machine. The
 * animation carries information rather than decoration: hue and motion encode
 * the assistant's state, the inner core responds to microphone level while
 * listening and to speech while speaking.
 */

const PALETTE: Record<AssistantState, { hue: number; speed: number; energy: number }> = {
  idle: { hue: 196, speed: 0.12, energy: 0.22 },
  listening: { hue: 186, speed: 0.5, energy: 0.85 },
  processing: { hue: 208, speed: 1.15, energy: 0.6 },
  speaking: { hue: 192, speed: 0.6, energy: 0.7 },
  executing: { hue: 268, speed: 0.95, energy: 0.65 },
  researching: { hue: 168, speed: 0.8, energy: 0.6 },
  awaiting_confirmation: { hue: 38, speed: 0.35, energy: 0.75 },
  error: { hue: 358, speed: 0.3, energy: 0.8 },
}

interface Particle { angle: number; radius: number; speed: number; size: number }

export function Core() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const stateRef = useRef<AssistantState>('idle')
  const levelRef = useRef(0)
  const speakingRef = useRef(false)
  const reducedRef = useRef(false)

  const assistantState = useStore((s) => s.assistantState)
  const voiceLevel = useStore((s) => s.voiceLevel)
  const speaking = useStore((s) => s.speaking)
  const reduced = useStore((s) => s.status?.config?.ui?.reduced_motion ?? false)

  stateRef.current = assistantState
  levelRef.current = voiceLevel
  speakingRef.current = speaking
  reducedRef.current = reduced

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const context = canvas.getContext('2d')
    if (!context) return

    const particles: Particle[] = Array.from({ length: 34 }, (_, i) => ({
      angle: (i / 34) * Math.PI * 2,
      radius: 0.62 + Math.random() * 0.26,
      speed: 0.15 + Math.random() * 0.5,
      size: 0.6 + Math.random() * 1.5,
    }))

    let frame = 0
    let raf = 0
    let smoothedLevel = 0
    let smoothedHue = PALETTE.idle.hue
    let smoothedEnergy = PALETTE.idle.energy

    const resize = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 2)
      const rect = canvas.getBoundingClientRect()
      canvas.width = Math.max(1, Math.floor(rect.width * ratio))
      canvas.height = Math.max(1, Math.floor(rect.height * ratio))
      context.setTransform(ratio, 0, 0, ratio, 0, 0)
    }
    resize()
    const observer = new ResizeObserver(resize)
    observer.observe(canvas)

    const draw = (time: number) => {
      raf = requestAnimationFrame(draw)
      frame++
      const state = stateRef.current
      const tone = PALETTE[state] ?? PALETTE.idle
      const still = reducedRef.current
      // Idle and reduced-motion redraw at a lower rate: there is nothing moving
      // that the eye can see, and this keeps the fans quiet.
      if ((state === 'idle' || still) && frame % 2 !== 0) return

      const rect = canvas.getBoundingClientRect()
      const width = rect.width
      const height = rect.height
      const cx = width / 2
      const cy = height / 2
      const base = Math.min(width, height) / 2

      smoothedHue += (tone.hue - smoothedHue) * 0.06
      smoothedEnergy += (tone.energy - smoothedEnergy) * 0.08
      const target = state === 'listening'
        ? Math.min(1, levelRef.current * 9)
        : speakingRef.current
          ? 0.45 + Math.sin(time / 150) * 0.22
          : 0.12
      smoothedLevel += (target - smoothedLevel) * 0.14

      const t = still ? 0 : time / 1000
      context.clearRect(0, 0, width, height)

      // --- ambient glow -------------------------------------------------
      const glow = context.createRadialGradient(cx, cy, base * 0.02, cx, cy, base * 0.78)
      const alpha = 0.07 + smoothedEnergy * 0.10 + smoothedLevel * 0.10
      glow.addColorStop(0, `hsla(${smoothedHue}, 90%, 62%, ${alpha})`)
      glow.addColorStop(0.45, `hsla(${smoothedHue}, 85%, 50%, ${alpha * 0.22})`)
      glow.addColorStop(1, 'hsla(210, 60%, 20%, 0)')
      context.fillStyle = glow
      context.fillRect(0, 0, width, height)

      // --- outer tick ring ----------------------------------------------
      const outer = base * 0.92
      context.save()
      context.translate(cx, cy)
      context.rotate(t * 0.05 * tone.speed)
      context.strokeStyle = `hsla(${smoothedHue}, 70%, 62%, 0.28)`
      context.lineWidth = 1
      for (let i = 0; i < 72; i++) {
        const angle = (i / 72) * Math.PI * 2
        const long = i % 6 === 0
        const inner = outer - (long ? 10 : 5)
        context.beginPath()
        context.moveTo(Math.cos(angle) * inner, Math.sin(angle) * inner)
        context.lineTo(Math.cos(angle) * outer, Math.sin(angle) * outer)
        context.globalAlpha = long ? 0.75 : 0.32
        context.stroke()
      }
      context.restore()
      context.globalAlpha = 1

      // --- rotating arcs --------------------------------------------------
      const arcs = [
        { radius: base * 0.78, from: 0.2, to: 1.5, speed: 0.38, width: 1.6, alpha: 0.55 },
        { radius: base * 0.68, from: 2.4, to: 3.7, speed: -0.52, width: 2.4, alpha: 0.7 },
        { radius: base * 0.56, from: 4.4, to: 5.3, speed: 0.75, width: 1.2, alpha: 0.4 },
      ]
      for (const arc of arcs) {
        const spin = t * arc.speed * tone.speed
        context.beginPath()
        context.strokeStyle = `hsla(${smoothedHue}, 92%, 66%, ${arc.alpha})`
        context.lineWidth = arc.width
        context.lineCap = 'round'
        context.arc(cx, cy, arc.radius, arc.from + spin, arc.to + spin)
        context.stroke()
      }

      // --- particles ------------------------------------------------------
      const busy = state === 'processing' || state === 'executing' || state === 'researching'
      if (busy && !still) {
        for (const particle of particles) {
          particle.angle += 0.004 * particle.speed * tone.speed * 6
          const radius = base * particle.radius
          const x = cx + Math.cos(particle.angle) * radius
          const y = cy + Math.sin(particle.angle) * radius
          context.beginPath()
          context.fillStyle = `hsla(${smoothedHue}, 95%, 72%, ${0.25 + particle.size * 0.2})`
          context.arc(x, y, particle.size, 0, Math.PI * 2)
          context.fill()
        }
      }

      // --- inner core -----------------------------------------------------
      const coreRadius = base * (0.13 + smoothedLevel * 0.085 + smoothedEnergy * 0.025)
      const coreGlow = context.createRadialGradient(cx, cy, 0, cx, cy, coreRadius * 1.9)
      coreGlow.addColorStop(0, `hsla(${smoothedHue}, 100%, 90%, 0.72)`)
      coreGlow.addColorStop(0.4, `hsla(${smoothedHue}, 95%, 62%, 0.26)`)
      coreGlow.addColorStop(1, `hsla(${smoothedHue}, 90%, 45%, 0)`)
      context.beginPath()
      context.fillStyle = coreGlow
      context.arc(cx, cy, coreRadius * 1.9, 0, Math.PI * 2)
      context.fill()

      // Two concentric rings: the outer one breathes with activity, the inner
      // one stays fixed so the scale of the movement is readable.
      context.beginPath()
      context.strokeStyle = `hsla(${smoothedHue}, 100%, 84%, 0.9)`
      context.lineWidth = 1.1
      context.arc(cx, cy, coreRadius, 0, Math.PI * 2)
      context.stroke()

      context.beginPath()
      context.strokeStyle = `hsla(${smoothedHue}, 90%, 70%, 0.35)`
      context.lineWidth = 0.8
      context.arc(cx, cy, base * 0.21, 0, Math.PI * 2)
      context.stroke()

      context.beginPath()
      context.fillStyle = `hsla(${smoothedHue}, 100%, 92%, ${0.5 + smoothedLevel * 0.4})`
      context.arc(cx, cy, Math.max(1.6, base * 0.012), 0, Math.PI * 2)
      context.fill()

      // --- waveform while speaking ----------------------------------------
      if (speakingRef.current && !still) {
        context.beginPath()
        context.strokeStyle = `hsla(${smoothedHue}, 100%, 78%, 0.7)`
        context.lineWidth = 1.4
        const span = base * 0.3
        for (let i = 0; i <= 48; i++) {
          const progress = i / 48
          const x = cx - span + progress * span * 2
          const envelope = Math.sin(progress * Math.PI)
          const y = cy + Math.sin(progress * 14 + t * 9) * envelope * base * 0.055
          i === 0 ? context.moveTo(x, y) : context.lineTo(x, y)
        }
        context.stroke()
      }
    }

    raf = requestAnimationFrame(draw)
    return () => {
      cancelAnimationFrame(raf)
      observer.disconnect()
    }
  }, [])

  return (
    <div className="core">
      <canvas ref={canvasRef} className="core__canvas" aria-hidden="true" />
      <div className="core__label">
        <span className="core__state" data-state={assistantState}>
          {LABELS[assistantState] ?? assistantState}
        </span>
      </div>
    </div>
  )
}

const LABELS: Record<string, string> = {
  idle: 'STANDBY',
  listening: 'LISTENING',
  processing: 'THINKING',
  speaking: 'SPEAKING',
  executing: 'EXECUTING',
  researching: 'RESEARCHING',
  awaiting_confirmation: 'AWAITING CONFIRMATION',
  error: 'FAULT',
}
