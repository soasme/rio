/- Behavioral model of the human durable transcript. This specifies publication
   and suffix selection, not Python execution, terminal layout, or SQLite commits. -/
namespace RioCli

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

def visibleCells : Event → List Cell
  | .accepted cells => cells
  | _ => []

theorem accepted_source_is_visible (id : Nat) (source : String) :
    visibleCells (.accepted [⟨id, source⟩]) = [⟨id, source⟩] := by rfl

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

end RioCli
