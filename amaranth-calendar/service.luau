-- 아마란스 일정 동기화 서비스: 설정을 인자로 바꿔 amaranth_sync.py(python3)를 주기 실행한다.
-- 종료 코드 0=성공, 1=다음 주기에 재시도, 2=사람이 설정·비밀번호를 고쳐야 함(자동 동기화 중단).

local running, failing, blocked = false, false, false
local pending, pendingManual = false, false
local warned = {}

local function cfg(key, default)
  local v = noctalia.getConfig(key)
  if v == nil or v == "" then return default end
  return v
end

local function vdirPath()
  return noctalia.expandPath(cfg("vdir_path", "~/.local/share/calendars/amaranth"))
end

local function warnOnce(key, body)
  if warned[key] then return end
  warned[key] = true
  noctalia.notify(noctalia.tr("notify." .. key), body)
end

-- noctalia 캘린더가 켜져 있고 이 vdir을 읽는 계정이 있는지. 플러그인은 noctalia 설정을 쓸 수 없어 안내만 한다.
local function calendarReady(vdir)
  if noctalia.getSetting("calendar.enabled") ~= true then return false end
  local accounts = noctalia.getSetting("calendar.account")
  if type(accounts) ~= "table" then return false end
  local want = vdir:gsub("/+$", "")
  for _, acc in pairs(accounts) do
    if type(acc) == "table" and acc.type == "vdir" and type(acc.path) == "string"
      and noctalia.expandPath(acc.path):gsub("/+$", "") == want then
      return true
    end
  end
  return false
end

local function checkSetup()
  if not noctalia.commandExists("python3") then
    warnOnce("need_python", noctalia.tr("notify.need_python_body"))
    return false
  end
  if cfg("server_url", "") == "" then
    warnOnce("need_server", noctalia.tr("notify.need_server_body"))
    if not noctalia.fileExists(vdirPath() .. "/.amaranth-sync.json") then noctalia.openSettings() end
    return false
  end
  if not calendarReady(vdirPath()) then
    warnOnce("need_calendar", noctalia.tr("notify.need_calendar_body") .. vdirPath())
  end
  return true
end

local function argv()
  local auth = cfg("auth_method", "password")
  local a = {
    "python3", noctalia.pluginDir() .. "/amaranth_sync.py",
    "--server", cfg("server_url", ""),
    "--vdir", vdirPath(),
    cfg("only_mine", true) and "--only-mine" or "--all",
    "--past-days", tostring(cfg("past_days", 14)),
    "--future-days", tostring(cfg("future_days", 60)),
    "--utc-offset", tostring(cfg("server_utc_offset", 9)),
    "--auth", auth,
  }
  if auth == "password" then
    table.insert(a, "--login-id"); table.insert(a, cfg("login_id", ""))
  else
    table.insert(a, "--browser"); table.insert(a, cfg("browser", "chrome"))
    local profile = cfg("browser_profile", "")
    if profile ~= "" then table.insert(a, "--profile"); table.insert(a, profile) end
  end
  return a
end

local function handleResult(r)
  local last = (r.stdout or ""):match("([^\n]*)\n*$") or ""
  local ok, res = pcall(noctalia.json.decode, last)
  local msg = (ok and type(res) == "table" and res.error) or (r.timedOut and "시간 초과") or (r.stderr or "")
  if r.exitCode == 0 then failing, blocked = false, false; return end
  if r.exitCode == 2 then blocked = true end
  if not failing or blocked then noctalia.notifyError(noctalia.tr("notify.failed"), msg) end
  failing = true
end

local function sync(manual)
  -- 실행 중에 들어온 요청(설정 변경 등)은 버리지 않고, 끝나는 즉시 최신 설정으로 한 번 더 실행한다.
  if running then pending, pendingManual = true, pendingManual or manual; return end
  if (blocked and not manual) or not checkSetup() then return end
  running = true
  local started = noctalia.runAsync(argv(), function(r)
    running = false
    handleResult(r)
    if pending then
      local again = pendingManual
      pending, pendingManual = false, false
      sync(again)
    end
  end, 120000)
  if not started then
    running = false
    warnOnce("need_python", noctalia.tr("notify.need_python_body"))
  end
end

local function schedule()
  noctalia.setUpdateInterval(math.max(5, tonumber(cfg("refresh_minutes", 15)) or 15) * 60000)
end

function update() sync(false) end
function onConfigChanged() blocked, failing = false, false; schedule(); sync(true) end
function onIpc(event) if event == "sync" then blocked = false; sync(true) end end

schedule()
sync(false)
