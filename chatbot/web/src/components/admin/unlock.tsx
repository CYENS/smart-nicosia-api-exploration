/* Unlock changes: asks once for the admin password and keeps it for this tab only
   (lib/collectors.ts). Shared by the collectors' admin page and the camera walkthrough. */
import { LockIcon } from "lucide-react"
import { useState, type FormEvent } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { unlockChanges } from "@/lib/collectors"

export function UnlockButton({ what, onUnlocked, size = "sm" }: {
  what: string // what the password unlocks, in a sentence
  onUnlocked?: () => void
  size?: "sm" | "default"
}) {
  const [open, setOpen] = useState(false)
  const [password, setPassword] = useState("")
  const [wrong, setWrong] = useState(false)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (busy || !password) return
    setBusy(true)
    const ok = await unlockChanges(password)
    setBusy(false)
    if (ok) {
      setOpen(false)
      setPassword("")
      setWrong(false)
      toast.success("Changes unlocked for this tab")
      onUnlocked?.()
    } else setWrong(true)
  }

  return (
    <>
      <Button variant="outline" size={size} onClick={() => setOpen(true)}>
        <LockIcon data-icon="inline-start" />
        Unlock changes
      </Button>
      <Dialog open={open} onOpenChange={(o) => { setOpen(o); if (!o) { setPassword(""); setWrong(false) } }}>
        <DialogContent className="sm:max-w-sm">
          <form onSubmit={submit} className="flex flex-col gap-4">
            <DialogHeader>
              <DialogTitle>Unlock changes</DialogTitle>
              <DialogDescription>{what}</DialogDescription>
            </DialogHeader>
            <Field data-invalid={wrong || undefined}>
              <FieldLabel htmlFor="admin-password">Admin password</FieldLabel>
              <Input
                id="admin-password" type="password" autoComplete="current-password" autoFocus
                value={password} onChange={(e) => { setPassword(e.target.value); setWrong(false) }}
                aria-invalid={wrong || undefined}
              />
              {wrong ? <FieldError>That password isn't right.</FieldError> : <FieldDescription>Kept for this tab only.</FieldDescription>}
            </Field>
            <DialogFooter>
              <DialogClose render={<Button variant="outline" type="button" />}>Cancel</DialogClose>
              <Button type="submit" aria-disabled={busy || !password || undefined}>
                {busy && <Spinner data-icon="inline-start" />}
                Unlock
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  )
}
