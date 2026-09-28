-- Hammerspoon: draw a ring around the Mac pointer so it shows in the VNC
-- mirror on the Frame. macOS Screen Sharing leaves the pointer out of the
-- framebuffer; a real on-screen window is captured like anything else.
--
-- Install: brew install --cask hammerspoon, then in ~/.hammerspoon/init.lua:
--   dofile("/path/to/frame-control/scripts/mac-cursor-ring.lua")
-- Toggle: ctrl+alt+cmd+M. Polls the pointer position, so no Accessibility
-- permission is needed.

local SIZE, WIDTH = 34, 3
local COLOR = { red = 1, green = 0.2, blue = 0.2, alpha = 0.9 }

local ring = hs.canvas.new({ x = 0, y = 0, w = SIZE, h = SIZE })
ring:appendElements({
  type = "circle", action = "stroke",
  strokeColor = COLOR, strokeWidth = WIDTH,
  radius = (SIZE - WIDTH) / 2,
})
ring:level(hs.canvas.windowLevels.cursor)
ring:behavior({ "canJoinAllSpaces", "stationary", "ignoresCycle" })

local last = {}
local function follow()
  local p = hs.mouse.absolutePosition()
  if p.x ~= last.x or p.y ~= last.y then
    ring:topLeft({ x = p.x - SIZE / 2, y = p.y - SIZE / 2 })
    last = p
  end
end

frameCursorRing = { canvas = ring, timer = hs.timer.new(1 / 60, follow) }

local function show() follow(); ring:show(); frameCursorRing.timer:start() end
local function hide() frameCursorRing.timer:stop(); ring:hide() end

hs.hotkey.bind({ "ctrl", "alt", "cmd" }, "M", function()
  if ring:isShowing() then hide() else show() end
end)

show()
