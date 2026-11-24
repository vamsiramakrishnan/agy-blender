// containment_press: written by `agy3d install`. A React Three Fiber component around the same loader.
//
//   import { ContainmentPress } from "./agy3d/containment_press/react";
//   <Canvas><ContainmentPress position={[0, 0, 0]} /></Canvas>
import { createElement, useEffect, useState } from "react";
import { useThree } from "@react-three/fiber";
import { loadContainmentPress } from "./index.js";

export function ContainmentPress({ baseUrl, transcoderPath, ktx2Loader, collision, levels, onLoad, onError, ...props }) {
  const gl = useThree((state) => state.gl);
  const [object, setObject] = useState(null);
  useEffect(() => {
    let cancelled = false;
    let loaded = null;
    loadContainmentPress({ renderer: gl, baseUrl, transcoderPath, ktx2Loader, collision, levels }).then(
      (group) => {
        if (cancelled) return group.userData.agy3d.dispose();
        loaded = group;
        setObject(group);
        if (onLoad) onLoad(group);
      },
      (error) => { if (onError) onError(error); else console.error(error); },
    );
    return () => { cancelled = true; if (loaded) loaded.userData.agy3d.dispose(); setObject(null); };
  }, [gl, baseUrl, transcoderPath, ktx2Loader, collision, levels]);
  return object ? createElement("primitive", { object, ...props }) : null;
}
