// The assistant's orb: a cloud of points on a sphere inside slowly turning ring arcs.
// Default export so the 3D library loads only when a screen that shows the orb is opened.

import { memo, useEffect, useMemo, useRef } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useReducedMotion } from "motion/react";
import { AdditiveBlending, Color, type Group } from "three";

export type OrbState = "idle" | "listening" | "thinking" | "speaking";

const FPS = 30;
const POINTS = 1400;
const POINT_SCALE = 0.04; // point size relative to canvas height
const CYAN = "#3fe0ff";

const KEYS = ["turn", "flow", "amp", "freq", "glow", "rings"] as const;
type Look = Record<(typeof KEYS)[number], number>;

/**
 * What each state looks like; the scene eases from one row to the next.
 * turn: cloud spin (rad/s) · flow: ripple speed · amp: ripple height · freq: ripple fineness ·
 * glow: brightness · rings: ring spin (rad/s)
 */
const LOOK: Record<OrbState, Look> = {
  idle: { turn: 0.15, flow: 0.6, amp: 0.03, freq: 2.2, glow: 0.55, rings: 0.2 },
  listening: { turn: 0.25, flow: 1.2, amp: 0.07, freq: 2.8, glow: 0.8, rings: 0.5 },
  thinking: { turn: 1.1, flow: 3.2, amp: 0.05, freq: 6.5, glow: 0.9, rings: 2.6 },
  speaking: { turn: 0.3, flow: 2.2, amp: 0.16, freq: 3.4, glow: 1, rings: 0.8 },
};

/** Ring arcs as [radius, arc length, tilt x, tilt y]. */
const RINGS = [
  [1.26, Math.PI * 1.6, Math.PI / 2, 0],
  [1.38, Math.PI * 1.15, 1.15, 0.5],
  [1.5, Math.PI * 0.7, 0.3, -0.45],
] as const;

const VERTEX = /* glsl */ `
  uniform float uPhase;
  uniform float uAmp;
  uniform float uFreq;
  uniform float uSize;
  attribute float aSeed;
  varying float vLight;

  void main() {
    // The points sit on a unit sphere, so each position is also its normal.
    float wave = sin(position.x * uFreq + uPhase) * sin(position.y * uFreq * 1.3 - uPhase * 0.8)
      + 0.5 * sin(position.z * uFreq * 0.7 + uPhase * 1.4);
    vec4 view = modelViewMatrix * vec4(position * (1.0 + uAmp * wave), 1.0);
    float facing = normalize(normalMatrix * position).z * 0.5 + 0.5;
    vLight = mix(0.3, 1.0, facing) * (0.75 + 0.25 * wave);
    gl_PointSize = uSize * (0.5 + aSeed) / -view.z;
    gl_Position = projectionMatrix * view;
  }
`;

const FRAGMENT = /* glsl */ `
  uniform vec3 uColor;
  uniform float uGlow;
  varying float vLight;

  void main() {
    float falloff = smoothstep(0.5, 0.0, length(gl_PointCoord - 0.5));
    gl_FragColor = vec4(uColor, falloff * vLight * uGlow);
    #include <colorspace_fragment>
  }
`;

/** Evenly spread points on a unit sphere (Fibonacci lattice), plus a per-point size seed. */
function spherePoints() {
  const positions = new Float32Array(POINTS * 3);
  const seeds = new Float32Array(POINTS);
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < POINTS; i++) {
    const y = 1 - (2 * (i + 0.5)) / POINTS;
    const radius = Math.sqrt(1 - y * y);
    positions.set([Math.cos(goldenAngle * i) * radius, y, Math.sin(goldenAngle * i) * radius], i * 3);
    seeds[i] = (i * 0.618034) % 1;
  }
  return { positions, seeds };
}

function Scene({ state, still }: { state: OrbState; still: boolean }) {
  const cloud = useRef<Group>(null);
  const rings = useRef<Group>(null);
  const invalidate = useThree((three) => three.invalidate);
  const look = useRef({ ...LOOK.idle, phase: 0, angle: 0, ringAngle: 0 });
  const { positions, seeds } = useMemo(spherePoints, []);
  // Built once: the renderer makes a new material whenever the object passed as `args` changes.
  const shader = useMemo(
    () => ({
      vertexShader: VERTEX,
      fragmentShader: FRAGMENT,
      uniforms: {
        uPhase: { value: 0 },
        uAmp: { value: 0 },
        uFreq: { value: 1 },
        uGlow: { value: 0 },
        uSize: { value: 1 },
        uColor: { value: new Color(CYAN) },
      },
    }),
    [],
  );
  const { uniforms } = shader;

  // The canvas renders on demand. Ask for at most FPS frames a second and none while the page
  // is hidden; under reduced motion, draw a single frame per state instead.
  useEffect(() => {
    if (still) {
      invalidate();
      return;
    }
    let frame = 0;
    let last = 0;
    const loop = (time: number) => {
      frame = requestAnimationFrame(loop);
      if (time - last < 1000 / FPS - 2) return;
      last = time;
      invalidate();
    };
    const sync = () => {
      cancelAnimationFrame(frame);
      if (!document.hidden) frame = requestAnimationFrame(loop);
    };
    sync();
    document.addEventListener("visibilitychange", sync);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("visibilitychange", sync);
    };
  }, [still, state, invalidate]);

  useFrame(({ size, viewport }, delta) => {
    const dt = Math.min(delta, 0.1);
    const now = look.current;
    const goal = LOOK[state];
    const ease = still ? 1 : 1 - Math.exp(-4 * dt);
    for (const key of KEYS) now[key] += (goal[key] - now[key]) * ease;
    if (!still) {
      now.phase += dt * now.flow;
      now.angle += dt * now.turn;
      now.ringAngle += dt * now.rings;
    }
    // Speech rhythm: while speaking, the ripple swells and settles instead of holding one height.
    const voice =
      state === "speaking" && !still ? 0.55 + 0.45 * Math.sin(now.phase * 3.1) * Math.sin(now.phase * 1.3) : 1;
    uniforms.uPhase.value = now.phase;
    uniforms.uAmp.value = now.amp * voice;
    uniforms.uFreq.value = now.freq;
    uniforms.uGlow.value = now.glow;
    uniforms.uSize.value = size.height * viewport.dpr * POINT_SCALE;
    if (cloud.current) cloud.current.rotation.y = now.angle;
    rings.current?.children.forEach((ring, i) => {
      ring.rotation.z = now.ringAngle * (i % 2 ? -1 : 1) * (1 + i * 0.4);
    });
  });

  return (
    <group rotation={[0.35, 0, 0.12]}>
      <group ref={cloud}>
        <points frustumCulled={false}>
          <bufferGeometry>
            <bufferAttribute attach="attributes-position" args={[positions, 3]} />
            <bufferAttribute attach="attributes-aSeed" args={[seeds, 1]} />
          </bufferGeometry>
          <shaderMaterial args={[shader]} blending={AdditiveBlending} depthWrite={false} transparent />
        </points>
        <mesh>
          <icosahedronGeometry args={[0.5, 1]} />
          <meshBasicMaterial color={CYAN} wireframe transparent opacity={0.22} />
        </mesh>
      </group>
      <group ref={rings}>
        {RINGS.map(([radius, arc, tiltX, tiltY], i) => (
          <mesh key={i} rotation={[tiltX, tiltY, 0]}>
            <torusGeometry args={[radius, 0.006, 6, 96, arc]} />
            <meshBasicMaterial color={CYAN} blending={AdditiveBlending} depthWrite={false} transparent opacity={0.55} />
          </mesh>
        ))}
      </group>
    </group>
  );
}

const CAMERA = { position: [0, 0, 4.8] as [number, number, number], fov: 40 };
const RENDERER = { alpha: true, antialias: true, powerPreference: "low-power" as const };
const FILL = { position: "absolute", inset: 0 } as const;

/** Memoised: the screen around it re-renders often, and the canvas should only hear about state changes. */
export default memo(function Orb({ state }: { state: OrbState }) {
  const still = Boolean(useReducedMotion());
  return (
    <Canvas flat frameloop="demand" dpr={[1, 2]} camera={CAMERA} gl={RENDERER} style={FILL}>
      <Scene state={state} still={still} />
    </Canvas>
  );
});
