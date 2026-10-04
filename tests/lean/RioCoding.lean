/- Behavioral models for rio.coding's edit validation, journal, and dispatch.

These are executable specifications of decisions made by the Python code, not
proofs about Python source. External I/O, hashes, and provider calls are inputs.
See README.md for the correspondence and limits.
-/
import RioCodingNotebook

namespace RioCoding

open RioCodingNotebook (Notebook Cell)

/- tools.py: edit validation. -/
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

/- session_store/tree.py: the notebook is rebuilt from the journal. A reset holds a
whole notebook; a step holds a patch, given here as the function it applies. -/
structure JournalEntry where
  id : String
  parent : Option String
  leafPointer : Option String := none
  reset : Option Notebook := none
  patch : Option (Notebook → Notebook) := none

def latestLeaf : List JournalEntry → Option String
  | [] => none
  | entries =>
      match (entries.reverse.find? (fun entry => entry.leafPointer.isSome)) with
      | some pointer => pointer.leafPointer
      | none => entries.getLast?.map (·.id)

def applyEntry (notebook : Notebook) (entry : JournalEntry) : Notebook :=
  match entry.reset, entry.patch with
  | some fresh, _ => fresh
  | none, some patch => patch notebook
  | none, none => notebook

/-- Replay a resolved root-to-leaf branch path onto an empty notebook. -/
def resumeNotebook (path : List JournalEntry) : Notebook := path.foldl applyEntry []

theorem empty_journal_has_empty_notebook : resumeNotebook [] = [] := by rfl

theorem steps_replay_in_order (history : List JournalEntry) (entry : JournalEntry)
    (patch : Notebook → Notebook) :
    resumeNotebook (history ++ [{ entry with reset := none, patch := some patch }]) =
      patch (resumeNotebook history) := by
  simp [resumeNotebook, applyEntry]

theorem a_reset_shadows_everything_before_it (history : List JournalEntry)
    (entry : JournalEntry) (fresh : Notebook) :
    resumeNotebook (history ++ [{ entry with reset := some fresh }]) = fresh := by
  simp [resumeNotebook, applyEntry]

theorem metadata_does_not_change_the_notebook (history : List JournalEntry) (id : String) :
    resumeNotebook (history ++ [{ id := id, parent := none }]) = resumeNotebook history := by
  simp [resumeNotebook, applyEntry]

/- session.py: a resumed run appends the new task to the journaled notebook. -/
def userCell (task : String) : Cell := { id := 0, code := false, source := task, outputs := [] }

def resumeWithTask (path : List JournalEntry) (task : String) : Notebook :=
  resumeNotebook path ++ [userCell task]

theorem resume_keeps_the_journaled_notebook (history : List JournalEntry)
    (entry : JournalEntry) (fresh : Notebook) (task : String) :
    resumeWithTask (history ++ [{ entry with reset := some fresh }]) task =
      fresh ++ [userCell task] := by
  simp [resumeWithTask, a_reset_shadows_everything_before_it]

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
