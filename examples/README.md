# Examples

```
python examples/offline_crate.py [OUT_DIR]                      # smallest offline prop -> package + inspector
agy3d prop "wooden crate with iron brackets" --offline --engine unreal_5 --out out/crate
agy3d sanitize a.glb b.obj c.usda --engine godot_4 --out out/clean
agy3d kit "ruined stone temple" --pieces 4 --offline --out out/kit [--splat bg.ply]
agy3d variants out/crate/package/Crate_authoring.usda --offline --out out/looks
agy3d calibrate labels.json --out studio_policy.yaml
agy3d models
agy3d plugin install [--target DIR] | agy3d plugin validate [PATH]
```

Drop `--offline` to use the real Gemini clients (`GEMINI_API_KEY`).
