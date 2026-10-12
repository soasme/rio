/- Behavioral model of the human durable transcript. This specifies publication
   and suffix selection, not Python execution, terminal layout, or SQLite commits. -/
namespace RioCli

/-- After handling help, parsing requires a command before dispatch. -/
def requireCommand {α : Type} : Option α → Except Nat α
  | none => .error 2
  | some command => .ok command

theorem bare_rio_exits_two : requireCommand (none : Option String) = .error 2 := rfl

theorem selected_command_is_dispatched (command : String) :
    requireCommand (some command) = .ok command := rfl

structure Cell where
  id : Nat
  body : String
  deriving DecidableEq, Repr

inductive Event where
  | accepted (newCells : List Cell)
  | proposed (cells : List Cell)
  | snapshot (cells : List Cell)
  | rejected
  deriving DecidableEq, Repr

def visibleCells : Event → List String
  | .accepted cells => cells.map Cell.body
  | _ => []

theorem accepted_source_is_visible (id : Nat) (source : String) :
    visibleCells (.accepted [⟨id, source⟩]) = [source] := by rfl

-- Command presentation joins quoted argv; quoting is supplied by Python shlex.
-- This models visibility, not shell parsing or execution.
theorem accepted_command_is_visible (id : Nat) (argv : List String)
    (quote : String → String) :
    visibleCells (.accepted [⟨id, String.intercalate " " (argv.map quote)⟩]) =
      [String.intercalate " " (argv.map quote)] := by rfl

theorem cell_ids_do_not_affect_content (first second : Nat) (body : String) :
    visibleCells (.accepted [⟨first, body⟩]) =
      visibleCells (.accepted [⟨second, body⟩]) := by rfl

theorem proposals_are_not_published (cells : List Cell) :
    visibleCells (.proposed cells) = [] := by rfl

theorem state_snapshots_do_not_repeat_cells (cells : List Cell) :
    visibleCells (.snapshot cells) = [] := by rfl

theorem rejected_patch_has_no_cells : visibleCells .rejected = [] := by rfl

-- Each stream tracks its own previous length. Durable snapshots only append.
def outputDelta (displayed : Nat) (snapshot : List Char) : List Char :=
  snapshot.drop displayed

theorem append_prints_only_new_output (old new : List Char) :
    outputDelta old.length (old ++ new) = new := by
  simp [outputDelta]

theorem repeated_snapshot_prints_nothing (snapshot : List Char) :
    outputDelta snapshot.length snapshot = [] := by
  simp [outputDelta]

-- OSC 7501 program status. The environment override wins over terminal detection.
def statusEnabled (override : Option Bool) (terminal : Bool) : Bool :=
  override.getD terminal

theorem override_forces_status (on terminal : Bool) :
    statusEnabled (some on) terminal = on := by rfl

theorem detection_without_override (terminal : Bool) :
    statusEnabled none terminal = terminal := by rfl

inductive Outcome where
  | succeeded
  | failed
  | interrupted
  deriving DecidableEq, Repr

-- A run reports `working`, then exactly one final state.
def reports : Outcome → List String
  | .succeeded => ["working", "done"]
  | .failed => ["working", "error"]
  | .interrupted => ["working", "idle"]

theorem run_starts_working (o : Outcome) : (reports o).head? = some "working" := by
  cases o <;> rfl

theorem run_reports_two_states (o : Outcome) : (reports o).length = 2 := by
  cases o <;> rfl

theorem interrupt_is_not_failure : reports .interrupted ≠ reports .failed := by decide

/-- Startup resolves only catalog entries. Invalid selection remains an error. -/
def resolveProvider (catalog : List (String × List String)) (name model : String) :
    Except String (String × String) :=
  match catalog.find? (fun entry => entry.1 == name) with
  | none => .error "unknown provider"
  | some entry =>
    if model ∈ entry.2 then .ok (name, model) else .error "unknown model"

theorem missing_provider_is_error (name model : String) :
    resolveProvider [] name model = .error "unknown provider" := rfl

theorem configured_model_is_selected (name model : String) :
    resolveProvider [(name, [model])] name model = .ok (name, model) := by
  simp [resolveProvider]

theorem invalid_model_does_not_choose_another_provider (name model : String) :
    resolveProvider [(name, [])] name model = .error "unknown model" := by
  simp [resolveProvider]

/-- Old model-cycling preferences are discarded while loading provider settings. -/
structure LegacyPreferences where
  defaultProvider : String
  defaultModel : String
  scopedModels : List (String × String)

def loadPreferences (saved : LegacyPreferences) : String × String :=
  (saved.defaultProvider, saved.defaultModel)

theorem legacy_scopes_do_not_change_selection (saved : LegacyPreferences)
    (scopes : List (String × String)) :
    loadPreferences { saved with scopedModels := scopes } = loadPreferences saved := rfl

theorem migration_preserves_defaults (provider model : String)
    (scopes : List (String × String)) :
    loadPreferences ⟨provider, model, scopes⟩ = (provider, model) := rfl

/-- A terminal resume recovers and replays; only an active run needs a provider. -/
def needsProvider (terminal : Bool) : Bool := !terminal

def canReplay (terminal cleanupSucceeded : Bool) : Bool := terminal && cleanupSucceeded

theorem terminal_replay_needs_no_provider : needsProvider true = false := rfl

theorem cleanup_failure_blocks_replay (terminal : Bool) :
    canReplay terminal false = false := by simp [canReplay]

theorem active_run_cannot_replay (cleanup : Bool) :
    canReplay false cleanup = false := rfl

/-- Durability metrics ride on the run-ended record, so ending is the only metrics write. -/
def endRun (journal : List String) : List String :=
  if "run_ended" ∈ journal then journal else journal ++ ["run_ended"]

theorem ending_twice_writes_once (journal : List String) :
    endRun (endRun journal) = endRun journal := by
  unfold endRun
  split <;> simp_all

/-- Replay appends only cleanup for leftover workers. -/
def replayJournal (journal : List String) (leftoverWorkers : Nat) : List String :=
  journal ++ List.replicate leftoverWorkers "worker_cleanup"

theorem clean_replay_writes_nothing (journal : List String) :
    replayJournal journal 0 = journal := by
  simp [replayJournal]

/-- Saved settings hold preferences only; provider definitions come from the catalog. -/
inductive SettingsFile where
  | preferences (names : List String)
  | legacyProviders
  deriving DecidableEq, Repr

def loadProviders (catalog : List String) : SettingsFile → Except String (List String)
  | .preferences names => .ok (names.filter (· ∈ catalog))
  | .legacyProviders => .error "move providers[] to catalog.toml"

theorem loaded_providers_are_catalog_providers (catalog names loaded : List String)
    (h : loadProviders catalog (.preferences names) = .ok loaded) :
    ∀ name ∈ loaded, name ∈ catalog := by
  simp [loadProviders] at h
  subst h
  intro name hm
  simpa using (List.mem_filter.mp hm).2

theorem legacy_provider_definitions_are_rejected (catalog : List String) :
    loadProviders catalog .legacyProviders = .error "move providers[] to catalog.toml" := rfl

/-- Saving preferences leaves the original catalog bytes, including comments, untouched. -/
structure ProviderFiles where
  catalog : String
  preferences : List String
  deriving DecidableEq, Repr

def savePreferences (files : ProviderFiles) (names : List String) : ProviderFiles :=
  { files with preferences := names }

theorem preference_save_preserves_catalog (files : ProviderFiles) (names : List String) :
    (savePreferences files names).catalog = files.catalog := rfl

/-- Login and subsequent requests use the same credential key without editing definitions. -/
def credentialKey (provider : String) (configured : Option String) : String :=
  configured.getD provider

theorem omitted_credential_key_uses_provider_name (provider : String) :
    credentialKey provider none = provider := rfl

theorem explicit_credential_key_is_preserved (provider key : String) :
    credentialKey provider (some key) = key := rfl

end RioCli
