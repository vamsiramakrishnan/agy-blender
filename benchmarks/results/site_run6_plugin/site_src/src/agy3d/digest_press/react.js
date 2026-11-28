// digest_press: written by `agy3d install`. A React Three Fiber component around the same loader.
//
//   import { DigestPress } from "./agy3d/digest_press/react";
//   <Canvas><DigestPress position={[0, 0, 0]} /></Canvas>
import { createElement, useEffect, useState } from "react";
import { useThree } from "@react-three/fiber";
import { loadDigestPress } from "./index.js";

export function DigestPress({ baseUrl, transcoderPath, ktx2Loader, collision, levels, onLoad, onError, ...props }) {
  const gl = useThree((state) => state.gl);
  const [object, setObject] = useState(null);
  useEffect(() => {
    let cancelled = false;
    let loaded = null;
    loadDigestPress({ renderer: gl, baseUrl, transcoderPath, ktx2Loader, collision, levels }).then(
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
