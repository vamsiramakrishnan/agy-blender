Request from the owner of the project, in their own words:

"Use antigravity, blender, shader and godot and three.js to generate a worldclass website for straitjacket, help me visualize how it works. https://www.scrolltide.co/ it scrolls so beautifully."

What that means here:

1. Straitjacket is an open-source tool for coding agents. Its repository is in ./straitjacket (README.md, docs/, evals/, assets/). Read it to learn what Straitjacket does and how it works. Do not modify anything inside ./straitjacket.
2. The website must help a visitor visualize how Straitjacket works, with scroll-driven 3D in the browser, of the quality of the reference site. The reference site cannot be opened from this machine; ./reference/ holds what it shows (read ./reference/README.md first). Design your own site for Straitjacket; do not copy the reference's design, art or words.
3. Use three.js and shaders for the 3D. Use whatever other current web libraries you judge right.
<!-- plugin -->
   Build the 3D objects the site needs with the antigravity-3d plugin attached to this session (it works with Blender and checks its output in Godot and three.js), and use what it delivers in the site.
<!-- /plugin -->
<!-- stock -->
   Build the 3D objects the site needs yourself: Blender 5.3 is installed (`blender` is on the PATH; run it headless, for example `blender -b --python script.py`), and glTF/GLB files are what three.js loads.
<!-- /stock -->

Where it will live: the site becomes the front page of Straitjacket's documentation site, a static site served under the path /straitjacket/. The documentation's own landing page is at ./docs/ relative to the front page.

Deliverable: the finished website in ./site/ with ./site/index.html as its entry.
- Everything the page loads (scripts, styles, 3D models, images, fonts, data) is a file inside ./site/ referenced by a relative URL. Nothing is loaded from another host.
- No server code. If you use a build tool, ./site/ must hold the built output and work from any path (relative base).
- It must work on a phone-width screen as well as on a desktop.

This machine:
- No internet access: nothing can be downloaded or installed, and URLs cannot be fetched.
- Node 22 and npm are installed. The packages listed in ./package.json are already in ./node_modules; no others are available.
- Python 3 is installed (for example `python3 -m http.server` serves files locally).
- A Chromium browser for headless use is at $CHROMIUM_BIN (it needs the flags --headless=new --no-sandbox).

Work until the site is finished and you have checked that it works. Then report: what you built, which libraries it uses and for what, which 3D objects you made and with what, how you checked it, and what you could not do.
