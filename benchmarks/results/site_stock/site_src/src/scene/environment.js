import * as THREE from 'three';

/**
 * Generates an in-memory HDR-like studio reflection environment via PMREMGenerator.
 * Gives brushed machined steel, dark obsidian titanium, and woven amber straps
 * crisp architectural softbox specular highlights without loading external HDR files.
 */
export function createStudioEnvironment(renderer) {
  const pmrem = new THREE.PMREMGenerator(renderer);
  pmrem.compileEquirectangularShader();

  const envScene = new THREE.Scene();
  envScene.background = new THREE.Color('#06080d');

  // Overhead wide cool-white precision light strip
  const topBox = new THREE.Mesh(
    new THREE.PlaneGeometry(18, 6),
    new THREE.MeshBasicMaterial({ color: new THREE.Color(2.4, 2.7, 3.2), side: THREE.DoubleSide })
  );
  topBox.position.set(0, 10, 0);
  topBox.rotation.x = Math.PI / 2;
  envScene.add(topBox);

  // Warm amber architectural side key strip (gives golden rim on brackets & clasps)
  const amberStrip = new THREE.Mesh(
    new THREE.PlaneGeometry(5, 14),
    new THREE.MeshBasicMaterial({ color: new THREE.Color(3.2, 1.9, 0.42), side: THREE.DoubleSide })
  );
  amberStrip.position.set(11, 2.5, 5);
  amberStrip.lookAt(0, 0, 0);
  envScene.add(amberStrip);

  // Cool cobalt rim strip on opposing left-rear flank
  const blueStrip = new THREE.Mesh(
    new THREE.PlaneGeometry(7, 13),
    new THREE.MeshBasicMaterial({ color: new THREE.Color(0.55, 1.35, 3.1), side: THREE.DoubleSide })
  );
  blueStrip.position.set(-12, 3.5, -6);
  blueStrip.lookAt(0, 0, 0);
  envScene.add(blueStrip);

  // Subtle floor bounce strip so underside bevels catch light cleanly
  const bounceStrip = new THREE.Mesh(
    new THREE.PlaneGeometry(16, 16),
    new THREE.MeshBasicMaterial({ color: new THREE.Color(0.22, 0.28, 0.38), side: THREE.DoubleSide })
  );
  bounceStrip.position.set(0, -10, 0);
  bounceStrip.rotation.x = -Math.PI / 2;
  envScene.add(bounceStrip);

  // Front fill accent panel for sleek bevel glints
  const frontAccent = new THREE.Mesh(
    new THREE.PlaneGeometry(6, 2.4),
    new THREE.MeshBasicMaterial({ color: new THREE.Color(1.6, 1.75, 2.0), side: THREE.DoubleSide })
  );
  frontAccent.position.set(-3, 4.5, 11);
  frontAccent.lookAt(0, 0, 0);
  envScene.add(frontAccent);

  const envRT = pmrem.fromScene(envScene, 0.04);
  pmrem.dispose();

  // Clean temporary geometry/materials
  envScene.traverse((obj) => {
    if (obj.isMesh) {
      obj.geometry.dispose();
      obj.material.dispose();
    }
  });

  return envRT.texture;
}
