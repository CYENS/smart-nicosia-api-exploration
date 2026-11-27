/* Where a collector's data comes from, kept out of the way until asked: a short "From ..." line
   (the same words the twin uses under its charts and layers) that opens a popover with the
   source's publisher, what it gives the twin, its licence and where to read more. */
import { ExternalLinkIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Popover, PopoverContent, PopoverDescription, PopoverHeader, PopoverTitle, PopoverTrigger } from "@/components/ui/popover"
import type { JobSource } from "@/lib/collectors"

export function SourceLink({ source }: { source: JobSource }) {
  return (
    <Popover>
      <PopoverTrigger
        render={<Button variant="link" size="xs" className="h-auto px-0" />}
        aria-label={`Where this comes from: ${source.name}`}
      >
        From {source.short ?? source.name}
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80">
        <PopoverHeader>
          <PopoverTitle>{source.name}</PopoverTitle>
          {source.publisher && <PopoverDescription>Published by the {source.publisher}</PopoverDescription>}
        </PopoverHeader>
        {source.gives && <p>{source.gives}</p>}
        <p className="text-muted-foreground">
          {source.licence ? (
            <>Licence: {source.licence_url
              ? <a className="underline underline-offset-4 hover:text-foreground" href={source.licence_url} target="_blank" rel="noreferrer">{source.licence}</a>
              : source.licence}</>
          ) : "Used under the publisher's own terms"}
        </p>
        {source.url && (
          <Button variant="outline" size="sm" nativeButton={false} render={<a href={source.url} target="_blank" rel="noreferrer" />}>
            Read about this source
            <ExternalLinkIcon data-icon="inline-end" />
            <span className="sr-only">(opens in a new tab)</span>
          </Button>
        )}
      </PopoverContent>
    </Popover>
  )
}
