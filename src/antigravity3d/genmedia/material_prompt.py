"""The words a material's texture is generated from: one builder for every caller.

The image model draws what it reads. A texture prompt therefore names the surface only (the material and its finish),
never the asset it belongs to: a brief such as "a supply crate with latches and handles" in an albedo prompt comes back
as a picture of crate panels with latches drawn in, which then tiles over every face of geometry that already models
them. The subject is the material's own ``surface`` words; a material without them (templates, documents written
before the field existed) is described from its id and, where the numbers settle it, its finish.
"""

from __future__ import annotations

import re

from antigravity3d.ir import MaterialIR

# Words that already say "metal": no "metal" is added after them.
_METALS = frozenset({"metal", "metallic", "steel", "iron", "brass", "bronze", "copper", "aluminium", "aluminum", "gold",
                     "silver", "chrome", "tin", "zinc", "nickel", "titanium", "pewter", "lead"})


def id_words(material_id: str) -> str:
    """``mat_olive_drab`` -> ``olive drab``; ``rustyIron_02`` -> ``rusty iron``. Drops a ``mat_`` / ``material_``
    prefix, a ``_mat`` / ``_material`` suffix and trailing numbers; splits camelCase and underscores."""
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", material_id).replace("_", " ").lower().split()
    if len(words) > 1 and words[0] in ("mat", "material"):
        words = words[1:]
    if len(words) > 1 and words[-1] in ("mat", "material"):
        words = words[:-1]
    while len(words) > 1 and words[-1].isdigit():
        words = words[:-1]
    return " ".join(words)


def finish_words(material: MaterialIR, words: str = "") -> str:
    """A finish only where metallic and roughness settle it: metallic >= 0.5 is a metal; roughness <= 0.3 polished
    (metal) or glossy, >= 0.7 rough (metal) or matte; a dielectric of middling roughness gets nothing."""
    r = material.roughness
    if material.metallic >= 0.5:
        adj = "polished" if r <= 0.3 else "rough" if r >= 0.7 else ""
        noun = "" if _METALS & set(words.split()) else "metal"
        return " ".join(w for w in (adj, noun) if w)
    return "glossy" if r <= 0.3 else "matte" if r >= 0.7 else ""


def texture_prompt(material: MaterialIR, brief: str = "") -> str:
    """The subject of a material's albedo prompt (``ImageForge.seamless_albedo`` wraps it in its template).

    ``brief``: the art direction's texture brief, appended as a sentence ('' appends nothing). The asset's own brief is
    never part of it (module docstring). No trailing full stop: the template adds one."""
    if material.surface:
        subject = material.surface
    else:
        words = id_words(material.id)
        finish = finish_words(material, words)
        subject = f"{words} surface" + (f", {finish}" if finish else "")
    # The forge's template puts its own full stop after this text, so neither part ends in one.
    brief = brief.strip().rstrip(" .")
    return subject.rstrip(" .,;:") + (f". {brief}" if brief else "")


__all__ = ["finish_words", "id_words", "texture_prompt"]
