// gl.js: the few lines of raw WebGL 2 the check_site fixtures share. Each fixture folder links its own copy
// (tests copy this file in), so every fixture is a self-contained site.
export function setup(canvas) {
  canvas.width = innerWidth; canvas.height = innerHeight;
  const gl = canvas.getContext('webgl2');
  if (!gl) return null;
  const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
  const prog = gl.createProgram();
  gl.attachShader(prog, sh(gl.VERTEX_SHADER, '#version 300 es\nin vec2 p; void main(){ gl_Position = vec4(p, 0.0, 1.0); }'));
  gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, '#version 300 es\nprecision mediump float; uniform vec3 col; out vec4 o; void main(){ o = vec4(col, 1.0); }'));
  gl.linkProgram(prog); gl.useProgram(prog);
  const col = gl.getUniformLocation(prog, 'col');
  const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
  // a quad covering the middle 60 % of the canvas each way: two triangles
  const quad = (pts) => gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(pts), gl.STATIC_DRAW);
  return { gl, quad, color: (r, g, b) => gl.uniform3f(col, r, g, b) };
}
export const QUAD = [-0.6, -0.6, 0.6, -0.6, 0.6, 0.6, -0.6, -0.6, 0.6, 0.6, -0.6, 0.6];
