import type { ReactNode } from "react";

// A table from md up; on a phone each row stacks, with the column name above each cell.
export function NotesTable({ columns, widths, rows }: { columns: string[]; widths: string; rows: { key: string; cells: ReactNode[] }[] }) {
  return (
    <div className="text-[13px]">
      <div className={`hidden gap-x-6 border-b border-line pb-2.5 md:grid ${widths}`}>
        {columns.map((c) => (
          <div key={c} className="label">
            {c}
          </div>
        ))}
      </div>
      {rows.map((row) => (
        <div key={row.key} className={`grid grid-cols-1 gap-x-6 gap-y-2 border-b border-line py-3 leading-snug md:gap-y-0 ${widths}`}>
          {row.cells.map((cell, i) => (
            <div key={i} className="min-w-0">
              {i > 0 && <div className="label mb-0.5 !text-[10px] md:hidden">{columns[i]}</div>}
              {cell}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
