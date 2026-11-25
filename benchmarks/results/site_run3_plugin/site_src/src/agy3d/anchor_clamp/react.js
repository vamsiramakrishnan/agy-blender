// anchor_clamp: written by `agy3d install`. A React Three Fiber component around the same loader.
//
//   import { AnchorClamp } from "./agy3d/anchor_clamp/react";
//   <Canvas><AnchorClamp position={[0, 0, 0]} /></Canvas>
import { createElement, useEffect, useState } from "react";
import { useThree } from "@react-three/fiber";
import { loadAnchorClamp } from "./index.js";

export function AnchorClamp({ baseUrl, transcoderPath, ktx2Loader, collision, onLoad, onError, ...props }) {
  const gl = useThree((state) => state.gl);
  const [object, setObject] = useState(null);
  useEffect(() => {
    let cancelled = false;
    let loaded = null;
    loadAnchorClamp({ renderer: gl, baseUrl, transcoderPath, ktx2Loader, collision }).then(
      (group) => {
        if (cancelled) return group.userData.agy3d.dispose();
        loaded = group;
        setObject(group);
        if (onLoad) onLoad(group);
      },
      (error) => { if (onError) onError(error); else console.error(error); },
    );
    return () => { cancelled = true; if (loaded) loaded.userData.agy3d.dispose(); setObject(null); };
  }, [gl, baseUrl, transcoderPath, ktx2Loader, collision]);
  return object ? createElement("primitive", { object, ...props }) : null;
}
