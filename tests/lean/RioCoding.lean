/- Behavioral models for rio.coding's tools, journal, and dispatch.

These are executable specifications of decisions made by the Python code, not
proofs about Python source. External I/O, hashes, and provider calls are inputs.
See README.md for the correspondence and limits.
-/
import RioAgent

namespace RioCoding

open RioAgent (Context)

/- tools.py: edit validation and terminal response decisions. -/
inductive EditError where
  | emptyOldText | notFound | duplicate | overlap | noChange
deriving DecidableEq, Repr

def validateEdit (emptyOld : Bool) (occurrences : Nat) (overlap changed : Bool) :
    Except EditError Unit :=
  if emptyOld then .error .emptyOldText
  else if occurrences == 0 then .error .notFound
  else if occurrences == 1 then
    if overlap then .error .overlap
    else if changed then .ok () else .error .noChange
  else .error .duplicate

theorem edit_requires_unique_match (n : Nat) (overlap changed : Bool)
    (h : 1 < n) : validateEdit false n overlap changed = .error .duplicate := by
  simp [validateEdit, Nat.ne_of_gt h, Nat.ne_of_gt (Nat.lt_trans (by decide : 0 < 1) h)]

theorem missing_text_rejected : validateEdit false 0 false true = .error .notFound := by rfl
theorem unchanged_edit_rejected : validateEdit false 1 false false = .error .noChange := by rfl
theorem unique_changed_edit_accepted : validateEdit false 1 false true = .ok () := by rfl

inductive CodingAction where
  | read | write | edit | bash | respond
deriving DecidableEq, Repr

inductive ToolOutcome where
  | observation | termination | rejected
deriving DecidableEq, Repr

/-- `respond` ends the run; an empty message is rejected as a failed action. -/
def toolOutcome (action : CodingAction) (emptyMessage : Bool) : ToolOutcome :=
  if action == .respond then
    if emptyMessage then .rejected else .termination
  else .observation

theorem respond_is_terminal : toolOutcome .respond false = .termination := by rfl
theorem empty_respond_is_rejected : toolOutcome .respond true = .rejected := by rfl
theorem other_actions_are_observations (empty : Bool) :
    toolOutcome .bash empty = .observation := by rfl

/- session_store/tree.py: explicit leaf pointers and latest branch checkpoint. -/
structure JournalEntry where
  id : String
  parent : Option String
  leafPointer : Option String := none
  snapshot : Option Context := none

def latestLeaf : List JournalEntry → Option String
  | [] => none
  | entries =>
      match (entries.reverse.find? (fun entry => entry.leafPointer.isSome)) with
      | some pointer => pointer.leafPointer
      | none => entries.getLast?.map (·.id)

def latestSnapshot : List JournalEntry → Option Context
  | [] => none
  | entry :: rest =>
      match latestSnapshot rest with
      | some context => some context
      | none => entry.snapshot

def resumeContext (path : List JournalEntry) : Context :=
  (latestSnapshot path).getD []

theorem empty_journal_has_empty_context : resumeContext [] = [] := by rfl

private theorem latestSnapshot_append_checkpoint (history : List JournalEntry)
    (entry : JournalEntry) (context : Context) :
    latestSnapshot (history ++ [{ entry with snapshot := some context }]) = some context := by
  induction history with
  | nil => rfl
  | cons first rest ih => simp [latestSnapshot, ih]

theorem latest_checkpoint_wins (history : List JournalEntry) (entry : JournalEntry)
    (context : Context) :
    resumeContext (history ++ [{ entry with snapshot := some context }]) = context := by
  simp [resumeContext, latestSnapshot_append_checkpoint]

theorem metadata_after_checkpoint_does_not_change_context (history : List JournalEntry)
    (entry : JournalEntry) (context : Context) :
    resumeContext (history ++ [{ entry with snapshot := some context },
      { id := "metadata", parent := some entry.id }]) = context := by
  have snapshot : latestSnapshot (history ++ [{ entry with snapshot := some context },
      { id := "metadata", parent := some entry.id }]) = some context := by
    induction history with
    | nil => rfl
    | cons first rest ih => simp [latestSnapshot, ih]
  simp [resumeContext, snapshot]

/- session.py: a resumed run appends the new task to the journaled context. -/
def resumeWithTask (path : List JournalEntry) (task : String) : Context :=
  resumeContext path ++ [{ role := "user", text := task }]

theorem resume_keeps_the_journaled_context (history : List JournalEntry) (entry : JournalEntry)
    (context : Context) (task : String) :
    resumeWithTask (history ++ [{ entry with snapshot := some context }]) task =
      context ++ [{ role := "user", text := task }] := by
  simp [resumeWithTask, latest_checkpoint_wins]

/- commands.py and thinking.py: command routing and mode cycling. -/
inductive Route where
  | prompt | skillPrompt | command (name : String) | unknownCommand
deriving DecidableEq, Repr

inductive ParsedInput where
  | text | skill (name : String) | slash (name : String)

def route (known : List String) : ParsedInput → Route
  | .text => .prompt
  | .skill _ => .skillPrompt
  | .slash name => if known.contains name then .command name else .unknownCommand

theorem ordinary_text_is_prompt (known : List String) : route known .text = .prompt := by rfl

theorem skill_command_is_prompt (known : List String) (name : String) :
    route known (.skill name) = .skillPrompt := by rfl

inductive Thinking where
  | off | minimal | low | medium | high | xhigh | max
deriving DecidableEq, Repr

def thinkingCycle : List Thinking :=
  [.off, .minimal, .low, .medium, .high, .xhigh, .max]

def nextThinking (available : List Thinking) (current : Thinking) : Thinking :=
  match available with
  | [] => .medium
  | first :: _ =>
      match (available.dropWhile (· != current)).drop 1 with
      | next :: _ => next
      | [] => first

theorem empty_thinking_set_uses_default (current : Thinking) :
    nextThinking [] current = .medium := by rfl

theorem thinking_wraps : nextThinking thinkingCycle .max = .off := by rfl

end RioCoding
