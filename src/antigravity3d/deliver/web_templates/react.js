// %%ASSET%%: written by `agy3d install`. A React Three Fiber component around the same loader.
//
//   import { %%COMPONENT%% } from "./agy3d/%%ASSET%%/react";
//   <Canvas><%%COMPONENT%% position={[0, 0, 0]} /></Canvas>
import { createElement, useEffect, useState } from "react";
import { useThree } from "@react-three/fiber";
import { %%LOAD%% } from "./index.js";

export function %%COMPONENT%%({ baseUrl, transcoderPath, ktx2Loader, collision, onLoad, onError, ...props }) {
  const gl = useThree((state) => state.gl);
  const [object, setObject] = useState(null);
  useEffect(() => {
    let cancelled = false;
    let loaded = null;
    %%LOAD%%({ renderer: gl, baseUrl, transcoderPath, ktx2Loader, collision }).then(
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
