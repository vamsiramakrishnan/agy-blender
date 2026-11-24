// capture_gate: written by `agy3d install`.
import type { Group } from "three";
import type { ReactElement } from "react";
import type { ThreeElements } from "@react-three/fiber";
import type { LoadOptions } from "./index";

export type CaptureGateProps = LoadOptions & {
  onLoad?: (group: Group) => void;
  onError?: (error: unknown) => void;
} & Omit<ThreeElements["group"], "object" | "ref">;

/** Loads the asset when mounted (needs the renderer of its Canvas) and disposes it when unmounted. */
export function CaptureGate(props: CaptureGateProps): ReactElement | null;
