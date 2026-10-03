/- Behavioral model of rio.coding.mcp: merging mcp.json files, and which
credential a request to an HTTP server carries.

Server configurations are opaque values; validation of individual entries is an
input (`valid`). See README.md for the correspondence and limits.
-/

namespace RioCodingMcp

/-- What a server needs to connect, reduced to what the decisions read. -/
structure Server where
  config : Nat          -- opaque command/url/headers
  enabled : Bool := true
  scope : String        -- "global" or "project"
deriving DecidableEq, Repr

/-- An `mcpServers` entry: a full definition, an `enabled`-only override, or an invalid one. -/
inductive Entry where
  | define (config : Nat) (enabled : Bool)
  | override (enabled : Bool)
  | invalid
deriving DecidableEq, Repr

abbrev Servers := List (String × Server)

def lookup (servers : Servers) (name : String) : Option Server :=
  (servers.find? (·.1 == name)).map (·.2)

def put (servers : Servers) (name : String) (server : Server) : Servers :=
  (servers.filter (·.1 != name)) ++ [(name, server)]

/-- config.py `_read`: one file's entries applied on top of what earlier files defined. -/
def applyEntry (scope : String) (servers : Servers) : String × Entry → Servers
  | (name, .define config enabled) => put servers name { config, enabled, scope }
  | (name, .override enabled) =>
      if scope == "project" then
        match lookup servers name with
        | some base => put servers name { base with enabled }
        | none => servers
      else servers
  | (_, .invalid) => servers

def readFile (scope : String) (servers : Servers) (entries : List (String × Entry)) : Servers :=
  entries.foldl (applyEntry scope) servers

/-- config.py `load_mcp_config`: the global file, then the project file only when trusted. -/
def load (global project : List (String × Entry)) (trusted : Bool) : Servers :=
  let base := readFile "global" [] global
  if trusted then readFile "project" base project else base

theorem untrusted_project_is_ignored (global project : List (String × Entry)) :
    load global project false = readFile "global" [] global := by
  simp [load]

theorem no_project_file_is_global_config (global : List (String × Entry)) (trusted : Bool) :
    load global [] trusted = readFile "global" [] global := by
  cases trusted <;> simp [load, readFile]

theorem invalid_entries_change_nothing (scope : String) (servers : Servers) (name : String) :
    applyEntry scope servers (name, .invalid) = servers := by
  rfl

theorem override_without_base_defines_nothing (servers : Servers) (name : String) (e : Bool)
    (h : lookup servers name = none) :
    applyEntry "project" servers (name, .override e) = servers := by
  simp [applyEntry, h]

theorem override_keeps_base_except_enabled (servers : Servers) (name : String) (e : Bool)
    (base : Server) (h : lookup servers name = some base) :
    applyEntry "project" servers (name, .override e) = put servers name { base with enabled := e } := by
  simp [applyEntry, h]

/-- Only project files may override; a global `enabled`-only entry is not a definition. -/
theorem global_override_is_ignored (servers : Servers) (name : String) (e : Bool) :
    applyEntry "global" servers (name, .override e) = servers := by
  simp [applyEntry]

theorem project_definition_replaces_global (servers : Servers) (name : String) (c : Nat) (e : Bool) :
    lookup (applyEntry "project" servers (name, .define c e)) name =
      some { config := c, enabled := e, scope := "project" } := by
  have h : (servers.filter (·.1 != name)).find? (·.1 == name) = none := by
    rw [List.find?_eq_none]
    intro x hx
    simp [List.mem_filter] at hx ⊢
    exact hx.2
  simp [applyEntry, put, lookup, List.find?_append, h]

/- connection.py and oauth.py: which bearer token a request carries. -/

/-- A configured `Authorization` header wins; otherwise the stored token, if it was issued
for exactly this server URL. -/
def bearer (headerAuth : Bool) (url : String) (stored : Option (String × String)) :
    Option String :=
  if headerAuth then none
  else match stored with
    | some (storedUrl, token) => if storedUrl == url then some token else none
    | none => none

theorem header_disables_oauth (url : String) (stored : Option (String × String)) :
    bearer true url stored = none := by
  rfl

theorem token_never_crosses_urls (url other token : String) (h : other ≠ url) :
    bearer false url (some (other, token)) = none := by
  simp [bearer, h]

theorem token_for_its_own_url (url token : String) :
    bearer false url (some (url, token)) = some token := by
  simp [bearer]

end RioCodingMcp
