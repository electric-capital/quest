// tableSchemaOps: every spec 4.4 operation derived from a draft diff, the
// ordering hazards (rename swaps and chains, rename + reorder, a new option
// as the default, removing the default's option, adding after a renamed
// column), no operations for an unchanged draft, every refused edit
// reported by validateDraft, the create request, and re-basing a draft.
//
// `applyLikeServer` replays derived operations with the server's own
// addressing rules (chat/tables/schema_ops.py: columns and options by their
// name at that point of the list, case-insensitive collisions, last column
// / last option, default_uses_option, multi_to_single), so each case also
// proves the list applies cleanly and lands on the draft.
import { describe, expect, it } from 'vitest';
import type { TableColumn, TableSchemaOperation } from '../api/types';
import {
  type DraftColumn,
  type SchemaDraft,
  deriveOperations,
  deriveSchemaChange,
  draftFromSchema,
  draftToCreateRequest,
  emptyDraft,
  newDraftColumn,
  newDraftId,
  foldName,
  rebaseDraft,
  schemaChangePhrase,
  settleReaddedOptions,
  summarizeSchemaChange,
  validateDraft,
} from './tableSchemaOps';

function col(key: string, name: string, overrides: Partial<TableColumn> = {}): TableColumn {
  return {
    key, name, type: 'text', required: false, unique: false, default: null, options: null,
    multiple: false, precision: null, description: '', ...overrides,
  };
}

const BEFORE: TableColumn[] = [
  col('c_00000001', 'Name'),
  col('c_00000002', 'Amount', { type: 'number', precision: 2 }),
  col('c_00000003', 'Status', { type: 'enum', options: ['open', 'won', 'lost'], default: 'open' }),
  col('c_00000004', 'Done', { type: 'boolean' }),
  col('c_00000005', 'Site', { type: 'url' }),
];

function draftOf(columns = BEFORE, title = 'Leads'): SchemaDraft {
  return draftFromSchema(title, columns);
}

function edit(draft: SchemaDraft, name: string, patch: Partial<DraftColumn>): SchemaDraft {
  return {
    ...draft,
    columns: draft.columns.map((c) => (c.name === name ? { ...c, ...patch } : c)),
  };
}

function find(draft: SchemaDraft, name: string): DraftColumn {
  const column = draft.columns.find((c) => c.name === name);
  if (!column) throw new Error(`no column ${name}`);
  return column;
}

const fold = foldName;

/** A small model of schema_ops.apply_schema_operations (names, options, order). */
function applyLikeServer(before: TableColumn[], ops: TableSchemaOperation[]): TableColumn[] {
  let columns = before.map((c) => ({ ...c, options: c.options ? [...c.options] : null }));
  const get = (name: string) => {
    const found = columns.find((c) => c.name === name);
    if (!found) throw new Error(`unknown_column ${name}`);
    return found;
  };
  const taken = (name: string, except?: string) =>
    columns.some((c) => c.key !== except && fold(c.name) === fold(name));
  let n = 0;
  for (const op of ops) {
    switch (op.op) {
      case 'rename_table':
        break;
      case 'drop_column': {
        const c = get(op.name);
        if (columns.length === 1) throw new Error('last_column');
        columns = columns.filter((x) => x !== c);
        break;
      }
      case 'rename_column': {
        const c = get(op.name);
        if (taken(op.new_name, c.key)) throw new Error(`duplicate_name ${op.new_name}`);
        c.name = op.new_name;
        break;
      }
      case 'reorder_columns': {
        const names = columns.map((c) => c.name).sort();
        if (JSON.stringify([...op.names].sort()) !== JSON.stringify(names)) throw new Error('not_a_permutation');
        columns = op.names.map(get);
        break;
      }
      case 'add_column': {
        if (taken(op.column.name)) throw new Error(`duplicate_name ${op.column.name}`);
        n += 1;
        const added = col(`c_new${n}`, op.column.name, {
          type: op.column.type, required: op.column.required ?? false, unique: op.column.unique ?? false,
          default: op.column.default ?? null, options: op.column.options ?? null,
          multiple: op.column.multiple ?? false, precision: op.column.precision ?? null,
          description: op.column.description ?? '',
        });
        if (op.after == null) columns.push(added);
        else columns.splice(columns.indexOf(get(op.after)) + 1, 0, added);
        break;
      }
      case 'set_description': get(op.name).description = op.description; break;
      case 'set_required': get(op.name).required = op.required; break;
      case 'set_unique': get(op.name).unique = op.unique; break;
      case 'set_precision': get(op.name).precision = op.precision; break;
      case 'set_default': {
        const c = get(op.name);
        if (c.type === 'enum' && op.default !== null) {
          const values = Array.isArray(op.default) ? op.default : [op.default as string];
          for (const v of values) if (!c.options!.includes(v)) throw new Error(`invalid_default ${v}`);
        }
        c.default = op.default;
        break;
      }
      case 'set_enum_multiple': {
        const c = get(op.name);
        if (!op.multiple && c.multiple) throw new Error('multi_to_single');
        if (op.multiple && !c.multiple) {
          c.multiple = true;
          c.default = c.default === null ? null : [c.default as string];
        }
        break;
      }
      case 'add_enum_option': {
        const c = get(op.name);
        if (c.options!.some((o) => fold(o) === fold(op.option))) throw new Error(`duplicate_option ${op.option}`);
        if (op.after == null) c.options!.push(op.option);
        else {
          const at = c.options!.indexOf(op.after);
          if (at < 0) throw new Error(`unknown_option ${op.after}`);
          c.options!.splice(at + 1, 0, op.option);
        }
        break;
      }
      case 'rename_enum_option': {
        const c = get(op.name);
        if (!c.options!.includes(op.option)) throw new Error(`unknown_option ${op.option}`);
        if (c.options!.some((o) => o !== op.option && fold(o) === fold(op.new_option))) {
          throw new Error(`duplicate_option ${op.new_option}`);
        }
        c.options = c.options!.map((o) => (o === op.option ? op.new_option : o));
        if (Array.isArray(c.default)) c.default = c.default.map((o) => (o === op.option ? op.new_option : o));
        else if (c.default === op.option) c.default = op.new_option;
        break;
      }
      case 'remove_enum_option': {
        const c = get(op.name);
        if (!c.options!.includes(op.option)) throw new Error(`unknown_option ${op.option}`);
        if (c.options!.length === 1) throw new Error('last_option');
        if (c.default === op.option || (Array.isArray(c.default) && c.default.includes(op.option))) {
          throw new Error('default_uses_option');
        }
        c.options = c.options!.filter((o) => o !== op.option);
        break;
      }
    }
  }
  return columns;
}

/** Derive, replay like the server, and compare with the expected final columns. */
function roundTrip(draft: SchemaDraft, before = BEFORE) {
  const ops = deriveOperations(before, 'Leads', draft);
  const after = applyLikeServer(before, ops);
  return { ops, after };
}

describe('deriveOperations: one operation per kind (spec 4.4)', () => {
  it('produces nothing for an unchanged draft', () => {
    expect(deriveOperations(BEFORE, 'Leads', draftOf())).toEqual([]);
  });

  it('ignores a number default spelled differently but equal', () => {
    const before = [col('c_1', 'N', { type: 'number', default: '1.1' })];
    const draft = edit(draftFromSchema('T', before), 'N', { defaultValue: '1.10' });
    expect(deriveOperations(before, 'T', draft)).toEqual([]);
  });

  const cases: [string, (d: SchemaDraft) => SchemaDraft, TableSchemaOperation[]][] = [
    ['rename_table', (d) => ({ ...d, title: '  Prospects ' }), [{ op: 'rename_table', title: 'Prospects' }]],
    [
      'rename_column',
      (d) => edit(d, 'Name', { name: 'Full name' }),
      [{ op: 'rename_column', name: 'Name', new_name: 'Full name' }],
    ],
    [
      'drop_column',
      (d) => ({ ...d, columns: d.columns.filter((c) => c.name !== 'Site') }),
      [{ op: 'drop_column', name: 'Site' }],
    ],
    [
      'reorder_columns',
      (d) => ({ ...d, columns: [d.columns[1], d.columns[0], ...d.columns.slice(2)] }),
      [{ op: 'reorder_columns', names: ['Amount', 'Name', 'Status', 'Done', 'Site'] }],
    ],
    [
      'set_description',
      (d) => edit(d, 'Name', { description: ' Who it is ' }),
      [{ op: 'set_description', name: 'Name', description: 'Who it is' }],
    ],
    ['set_required', (d) => edit(d, 'Name', { required: true }), [{ op: 'set_required', name: 'Name', required: true }]],
    ['set_unique', (d) => edit(d, 'Site', { unique: true }), [{ op: 'set_unique', name: 'Site', unique: true }]],
    [
      'set_default (text)',
      (d) => edit(d, 'Name', { defaultValue: 'Anon' }),
      [{ op: 'set_default', name: 'Name', default: 'Anon' }],
    ],
    [
      'set_default (boolean)',
      (d) => edit(d, 'Done', { defaultValue: 'false' }),
      [{ op: 'set_default', name: 'Done', default: false }],
    ],
    [
      'set_default (clear)',
      (d) => edit(d, 'Status', { defaultOptionIds: [] }),
      [{ op: 'set_default', name: 'Status', default: null }],
    ],
    ['set_precision', (d) => edit(d, 'Amount', { precision: '' }), [{ op: 'set_precision', name: 'Amount', precision: null }]],
    [
      'set_enum_multiple',
      (d) => edit(d, 'Status', { multiple: true }),
      // The wrapped default ["open"] is what the draft wants: no set_default.
      [{ op: 'set_enum_multiple', name: 'Status', multiple: true }],
    ],
    [
      'add_enum_option (after the last)',
      (d) => {
        const s = find(d, 'Status');
        return edit(d, 'Status', { options: [...s.options, { id: newDraftId('opt'), original: null, value: 'paused' }] });
      },
      [{ op: 'add_enum_option', name: 'Status', option: 'paused', after: 'lost' }],
    ],
    [
      'rename_enum_option',
      (d) => {
        const s = find(d, 'Status');
        return edit(d, 'Status', { options: s.options.map((o) => (o.value === 'won' ? { ...o, value: 'Won' } : o)) });
      },
      [{ op: 'rename_enum_option', name: 'Status', option: 'won', new_option: 'Won' }],
    ],
    [
      'remove_enum_option',
      (d) => {
        const s = find(d, 'Status');
        return edit(d, 'Status', { options: s.options.filter((o) => o.value !== 'lost') });
      },
      [{ op: 'remove_enum_option', name: 'Status', option: 'lost' }],
    ],
    [
      'add_column (after)',
      (d) => {
        const added = { ...newDraftColumn('Notes', 'text'), description: 'Free text' };
        return { ...d, columns: [d.columns[0], added, ...d.columns.slice(1)] };
      },
      [{ op: 'add_column', column: { name: 'Notes', type: 'text', description: 'Free text' }, after: 'Name' }],
    ],
  ];

  it.each(cases)('%s', (_label, change, expected) => {
    const draft = change(draftOf());
    const ops = deriveOperations(BEFORE, 'Leads', draft);
    expect(ops).toEqual(expected);
    // Replays cleanly.
    applyLikeServer(BEFORE, ops);
  });

  it('add_column carries the full spec (enum, multiple, default, required, unique, precision)', () => {
    const tags: DraftColumn = {
      ...newDraftColumn('Tags', 'enum'),
      options: [
        { id: 'o-a', original: null, value: 'a' },
        { id: 'o-b', original: null, value: 'b' },
      ],
      multiple: true,
      defaultOptionIds: ['o-b', 'o-a'],
      required: true,
    };
    const price: DraftColumn = { ...newDraftColumn('Price', 'number'), precision: '2', unique: true, defaultValue: ' 3.5 ' };
    const draft = { ...draftOf(), columns: [...draftOf().columns, tags, price] };
    expect(deriveOperations(BEFORE, 'Leads', draft)).toEqual([
      {
        op: 'add_column',
        column: { name: 'Tags', type: 'enum', required: true, options: ['a', 'b'], multiple: true, default: ['a', 'b'] },
        after: 'Site',
      },
      {
        op: 'add_column',
        column: { name: 'Price', type: 'number', unique: true, precision: 2, default: '3.5' },
        after: 'Tags',
      },
    ]);
  });
});

describe('deriveOperations: ordering', () => {
  it('swaps two column names through a temporary name', () => {
    let draft = edit(draftOf(), 'Name', { name: 'Site' });
    draft = { ...draft, columns: draft.columns.map((c) => (c.key === 'c_00000005' ? { ...c, name: 'Name' } : c)) };
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([
      { op: 'rename_column', name: 'Name', new_name: 'Renaming column 1' },
      { op: 'rename_column', name: 'Site', new_name: 'Name' },
      { op: 'rename_column', name: 'Renaming column 1', new_name: 'Site' },
    ]);
    expect(after.map((c) => [c.key, c.name])).toEqual([
      ['c_00000001', 'Site'], ['c_00000002', 'Amount'], ['c_00000003', 'Status'], ['c_00000004', 'Done'], ['c_00000005', 'Name'],
    ]);
  });

  it('orders a rename chain (A -> B while B -> C) without a temporary name', () => {
    let draft = edit(draftOf(), 'Amount', { name: 'Total' });
    draft = { ...draft, columns: draft.columns.map((c) => (c.key === 'c_00000001' ? { ...c, name: 'amount' } : c)) };
    const { ops } = roundTrip(draft);
    expect(ops).toEqual([
      { op: 'rename_column', name: 'Amount', new_name: 'Total' },
      { op: 'rename_column', name: 'Name', new_name: 'amount' },
    ]);
  });

  it('frees a dropped name before a rename takes it', () => {
    let draft: SchemaDraft = { ...draftOf(), columns: draftOf().columns.filter((c) => c.name !== 'Site') };
    draft = edit(draft, 'Name', { name: 'site' });
    expect(roundTrip(draft).ops).toEqual([
      { op: 'drop_column', name: 'Site' },
      { op: 'rename_column', name: 'Name', new_name: 'site' },
    ]);
  });

  it('a case-only rename is one step', () => {
    expect(roundTrip(edit(draftOf(), 'Name', { name: 'NAME' })).ops).toEqual([
      { op: 'rename_column', name: 'Name', new_name: 'NAME' },
    ]);
  });

  it('rename + reorder: reorder_columns uses the new names, last', () => {
    let draft = edit(draftOf(), 'Name', { name: 'Who' });
    draft = { ...draft, columns: [...draft.columns.slice(1), draft.columns[0]] };
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([
      { op: 'rename_column', name: 'Name', new_name: 'Who' },
      { op: 'reorder_columns', names: ['Amount', 'Status', 'Done', 'Site', 'Who'] },
    ]);
    expect(after.map((c) => c.name)).toEqual(['Amount', 'Status', 'Done', 'Site', 'Who']);
  });

  it('adds a column after a renamed column by its new name', () => {
    let draft = edit(draftOf(), 'Amount', { name: 'Total' });
    draft = { ...draft, columns: [draft.columns[0], draft.columns[1], newDraftColumn('Tax', 'number'), ...draft.columns.slice(2)] };
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([
      { op: 'rename_column', name: 'Amount', new_name: 'Total' },
      { op: 'add_column', column: { name: 'Tax', type: 'number' }, after: 'Total' },
    ]);
    expect(after.map((c) => c.name)).toEqual(['Name', 'Total', 'Tax', 'Status', 'Done', 'Site']);
  });

  it('a new first column is appended, then reordered into place', () => {
    const draft = { ...draftOf(), columns: [newDraftColumn('First', 'text'), ...draftOf().columns] };
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([
      { op: 'add_column', column: { name: 'First', type: 'text' } },
      { op: 'reorder_columns', names: ['First', 'Name', 'Amount', 'Status', 'Done', 'Site'] },
    ]);
    expect(after[0].name).toBe('First');
  });

  it('a new option used as the default is added before set_default', () => {
    const d = draftOf();
    const s = find(d, 'Status');
    const draft = edit(d, 'Status', {
      options: [...s.options, { id: 'o-new', original: null, value: 'paused' }],
      defaultOptionIds: ['o-new'],
    });
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([
      { op: 'add_enum_option', name: 'Status', option: 'paused', after: 'lost' },
      { op: 'set_default', name: 'Status', default: 'paused' },
    ]);
    expect(after[2].default).toBe('paused');
  });

  it("removing the default's option moves the default to a kept option first (one set_default)", () => {
    const d = draftOf();
    const s = find(d, 'Status');
    const won = s.options.find((o) => o.value === 'won')!;
    const draft = edit(d, 'Status', { options: s.options.filter((o) => o.value !== 'open'), defaultOptionIds: [won.id] });
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([
      { op: 'set_default', name: 'Status', default: 'won' },
      { op: 'remove_enum_option', name: 'Status', option: 'open' },
    ]);
    expect(after[2]).toMatchObject({ options: ['won', 'lost'], default: 'won' });
  });

  it("removing the default's option for a kept option being renamed: set by the loaded spelling, the rename carries it", () => {
    const d = draftOf();
    const s = find(d, 'Status');
    const won = s.options.find((o) => o.value === 'won')!;
    const draft = edit(d, 'Status', {
      options: s.options.filter((o) => o.value !== 'open').map((o) => (o.id === won.id ? { ...o, value: 'Won' } : o)),
      defaultOptionIds: [won.id],
    });
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([
      { op: 'set_default', name: 'Status', default: 'won' },
      { op: 'remove_enum_option', name: 'Status', option: 'open' },
      { op: 'rename_enum_option', name: 'Status', option: 'won', new_option: 'Won' },
    ]);
    expect(after[2]).toMatchObject({ options: ['Won', 'lost'], default: 'Won' });
  });

  it("removing the default's option for a NEW default: none first, the new default after the add", () => {
    const d = draftOf();
    const s = find(d, 'Status');
    const draft = edit(d, 'Status', {
      options: [...s.options.filter((o) => o.value !== 'open'), { id: 'o-new', original: null, value: 'paused' }],
      defaultOptionIds: ['o-new'],
    });
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([
      { op: 'set_default', name: 'Status', default: null },
      { op: 'remove_enum_option', name: 'Status', option: 'open' },
      { op: 'add_enum_option', name: 'Status', option: 'paused', after: 'lost' },
      { op: 'set_default', name: 'Status', default: 'paused' },
    ]);
    expect(after[2]).toMatchObject({ options: ['won', 'lost', 'paused'], default: 'paused' });
  });

  it('a three-way rename cycle parks one column only', () => {
    const rename: Record<string, string> = { c_00000001: 'Amount', c_00000002: 'Site', c_00000005: 'Name' };
    const draft = { ...draftOf(), columns: draftOf().columns.map((c) => (rename[c.id] ? { ...c, name: rename[c.id] } : c)) };
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([
      { op: 'rename_column', name: 'Name', new_name: 'Renaming column 1' },
      { op: 'rename_column', name: 'Site', new_name: 'Name' },
      { op: 'rename_column', name: 'Amount', new_name: 'Site' },
      { op: 'rename_column', name: 'Renaming column 1', new_name: 'Amount' },
    ]);
    expect(after.map((c) => c.name)).toEqual(['Amount', 'Site', 'Status', 'Done', 'Name']);
  });

  it('an invalid draft (two columns named alike) still derives, and ends', () => {
    const draft = edit(draftOf(), 'Done', { name: 'name' });
    expect(deriveOperations(BEFORE, 'Leads', draft)).toEqual([
      { op: 'rename_column', name: 'Done', new_name: 'name' },
    ]);
  });

  it('replacing a column by a new one of the same name frees the name first', () => {
    const fresh = newDraftColumn('site', 'number');
    const draft = { ...draftOf(), columns: [...draftOf().columns.filter((c) => c.name !== 'Site'), fresh] };
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([
      { op: 'drop_column', name: 'Site' },
      { op: 'add_column', column: { name: 'site', type: 'number' }, after: 'Done' },
    ]);
    expect(after.map((c) => [c.name, c.type])).toContainEqual(['site', 'number']);
  });

  it('replacing every column: one drop waits until the first new column is in (last_column)', () => {
    const before = [col('c_00000001', 'A'), col('c_00000002', 'B')];
    const c = newDraftColumn('C', 'text');
    const dcol = newDraftColumn('D', 'url');
    const draft: SchemaDraft = { title: 'Leads', columns: [c, dcol] };
    const { operations, targets } = deriveSchemaChange(before, 'Leads', draft);
    expect(operations).toEqual([
      { op: 'drop_column', name: 'A' },
      { op: 'add_column', column: { name: 'C', type: 'text' } },
      { op: 'drop_column', name: 'B' },
      { op: 'add_column', column: { name: 'D', type: 'url' }, after: 'C' },
    ]);
    expect(targets).toEqual(['c_00000001', c.id, 'c_00000002', dcol.id]);
    expect(applyLikeServer(before, operations).map((x) => x.name)).toEqual(['C', 'D']);
  });

  it('replacing the only column by one of the same name (a type change by hand) parks the old one', () => {
    const before = [col('c_00000001', 'Price')];
    const draft: SchemaDraft = { title: 'Leads', columns: [newDraftColumn('price', 'number')] };
    const ops = deriveOperations(before, 'Leads', draft);
    expect(ops).toEqual([
      { op: 'rename_column', name: 'Price', new_name: 'Removing column 1' },
      { op: 'add_column', column: { name: 'price', type: 'number' } },
      { op: 'drop_column', name: 'Removing column 1' },
    ]);
    expect(applyLikeServer(before, ops).map((x) => [x.name, x.type])).toEqual([['price', 'number']]);
  });

  it('replacing every option: one removal waits until the first new option is in (last_option)', () => {
    const before = [col('c_00000001', 'Name'), col('c_00000002', 'One', { type: 'enum', options: ['x', 'y'] })];
    const draft = edit(draftFromSchema('Leads', before), 'One', {
      options: [
        { id: 'o-1', original: null, value: 'p' },
        { id: 'o-2', original: null, value: 'q' },
      ],
    });
    const { ops, after } = roundTrip(draft, before);
    expect(ops).toEqual([
      { op: 'remove_enum_option', name: 'One', option: 'x' },
      { op: 'add_enum_option', name: 'One', option: 'p' },
      { op: 'remove_enum_option', name: 'One', option: 'y' },
      { op: 'add_enum_option', name: 'One', option: 'q', after: 'p' },
    ]);
    expect(after[1].options).toEqual(['p', 'q']);
  });

  it('replacing the only option by a spelling it takes (x -> X) is a rename (the option re-typed)', () => {
    const before = [col('c_00000001', 'One', { type: 'enum', options: ['x'] })];
    const draft = edit(draftFromSchema('Leads', before), 'One', { options: [{ id: 'o-1', original: null, value: 'X' }] });
    const { ops, after } = roundTrip(draft, before);
    expect(ops).toEqual([{ op: 'rename_enum_option', name: 'One', option: 'x', new_option: 'X' }]);
    expect(after[0].options).toEqual(['X']);
  });

  it('renaming the default option keeps it as the default without set_default', () => {
    const d = draftOf();
    const s = find(d, 'Status');
    const draft = edit(d, 'Status', { options: s.options.map((o) => (o.value === 'open' ? { ...o, value: 'Open' } : o)) });
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([{ op: 'rename_enum_option', name: 'Status', option: 'open', new_option: 'Open' }]);
    expect(after[2].default).toBe('Open');
  });

  it('swaps two option spellings through a temporary option', () => {
    const d = draftOf();
    const s = find(d, 'Status');
    const draft = edit(d, 'Status', {
      options: s.options.map((o) => (o.value === 'won' ? { ...o, value: 'lost' } : o.value === 'lost' ? { ...o, value: 'won' } : o)),
    });
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([
      { op: 'rename_enum_option', name: 'Status', option: 'won', new_option: 'Renaming option 1' },
      { op: 'rename_enum_option', name: 'Status', option: 'lost', new_option: 'won' },
      { op: 'rename_enum_option', name: 'Status', option: 'Renaming option 1', new_option: 'lost' },
    ]);
    expect(after[2].options).toEqual(['open', 'lost', 'won']);
  });

  it('a removed option frees its spelling for an added one elsewhere in the list', () => {
    const d = draftOf();
    const s = find(d, 'Status');
    const draft = edit(d, 'Status', {
      options: [...s.options.filter((o) => o.value !== 'won'), { id: 'o-x', original: null, value: 'WON' }],
    });
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([
      { op: 'remove_enum_option', name: 'Status', option: 'won' },
      { op: 'add_enum_option', name: 'Status', option: 'WON', after: 'lost' },
    ]);
    expect(after[2].options).toEqual(['open', 'lost', 'WON']);
  });

  it('a removed option typed again in its place is the option kept: no operation', () => {
    const d = draftOf();
    const s = find(d, 'Status');
    const draft = edit(d, 'Status', {
      options: [...s.options.filter((o) => o.value !== 'lost'), { id: 'o-x', original: null, value: 'lost' }],
    });
    expect(deriveOperations(BEFORE, 'Leads', draft)).toEqual([]);
  });

  it('a removed option typed again in another case, in its place, is a rename', () => {
    const d = draftOf();
    const s = find(d, 'Status');
    const draft = edit(d, 'Status', {
      options: [...s.options.filter((o) => o.value !== 'lost'), { id: 'o-x', original: null, value: 'LOST' }],
    });
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([{ op: 'rename_enum_option', name: 'Status', option: 'lost', new_option: 'LOST' }]);
    expect(after[2].options).toEqual(['open', 'won', 'LOST']);
  });

  it('a new option ahead of a re-typed one keeps the re-typed one from being claimed (it would be appended)', () => {
    const before = [col('c_00000001', 'One', { type: 'enum', options: ['w'] })];
    const draft = edit(draftFromSchema('Leads', before), 'One', {
      options: [
        { id: 'o-1', original: null, value: 'X' },
        { id: 'o-2', original: null, value: 'w' },
      ],
    });
    const { after } = roundTrip(draft, before);
    expect(after[0].options).toEqual(['X', 'w']);
  });

  it('single -> multi with a new default set wraps first, then sets the list', () => {
    const d = draftOf();
    const s = find(d, 'Status');
    const draft = edit(d, 'Status', { multiple: true, defaultOptionIds: [s.options[2].id, s.options[0].id] });
    const { ops, after } = roundTrip(draft);
    expect(ops).toEqual([
      { op: 'set_enum_multiple', name: 'Status', multiple: true },
      { op: 'set_default', name: 'Status', default: ['open', 'lost'] },
    ]);
    expect(after[2]).toMatchObject({ multiple: true, default: ['open', 'lost'] });
  });

  it('set_precision comes before set_default; edits on a renamed column use its new name', () => {
    const draft = edit(draftOf(), 'Amount', { name: 'Total', precision: '0', defaultValue: '5' });
    expect(roundTrip(draft).ops).toEqual([
      { op: 'rename_column', name: 'Amount', new_name: 'Total' },
      { op: 'set_precision', name: 'Total', precision: 0 },
      { op: 'set_default', name: 'Total', default: '5' },
    ]);
  });

  it('maps each operation to its draft column', () => {
    let draft = edit(draftOf(), 'Name', { required: true });
    draft = { ...draft, title: 'X', columns: [...draft.columns.filter((c) => c.name !== 'Done'), newDraftColumn('New', 'text')] };
    const { operations, targets } = deriveSchemaChange(BEFORE, 'Leads', draft);
    expect(operations.map((o) => o.op)).toEqual(['rename_table', 'drop_column', 'set_required', 'add_column']);
    expect(targets).toEqual([null, 'c_00000004', 'c_00000001', draft.columns[draft.columns.length - 1].id]);
  });
});

describe('settleReaddedOptions', () => {
  const status = BEFORE[2];

  it('turns a re-typed removed option back into the loaded one, moved to its place', () => {
    const d = draftOf();
    const s = find(d, 'Status');
    const column = {
      ...s,
      options: [...s.options.filter((o) => o.value !== 'won'), { id: 'o-x', original: null, value: 'won' }],
    };
    const settled = settleReaddedOptions(column, status);
    expect(settled.options.map((o) => [o.value, o.original])).toEqual([
      ['open', 'open'], ['won', 'won'], ['lost', 'lost'],
    ]);
    // The option keeps its draft id (React key) and the draft derives nothing.
    expect(settled.options[1].id).toBe('o-x');
    expect(deriveOperations(BEFORE, 'Leads', edit(d, 'Status', settled))).toEqual([]);
  });

  it('a case change settles as a rename, other new options stay after the loaded ones', () => {
    const d = draftOf();
    const s = find(d, 'Status');
    const column = {
      ...s,
      options: [
        ...s.options.filter((o) => o.value !== 'open'),
        { id: 'o-n', original: null, value: 'paused' },
        { id: 'o-x', original: null, value: 'Open' },
      ],
      defaultOptionIds: [s.options[1].id],
    };
    const settled = settleReaddedOptions(column, status);
    expect(settled.options.map((o) => o.value)).toEqual(['Open', 'won', 'lost', 'paused']);
    const { ops, after } = roundTrip(edit(d, 'Status', settled));
    expect(ops).toEqual([
      { op: 'rename_enum_option', name: 'Status', option: 'open', new_option: 'Open' },
      { op: 'add_enum_option', name: 'Status', option: 'paused', after: 'lost' },
      { op: 'set_default', name: 'Status', default: 'won' },
    ]);
    expect(after[2].options).toEqual(['Open', 'won', 'lost', 'paused']);
  });

  it('returns the column itself when nothing was re-typed (or it is not a loaded enum)', () => {
    const s = find(draftOf(), 'Status');
    expect(settleReaddedOptions(s, status)).toBe(s);
    expect(settleReaddedOptions(s, null)).toBe(s);
  });
});

describe('summarizeSchemaChange / schemaChangePhrase', () => {
  const summary = (draft: SchemaDraft) => summarizeSchemaChange(deriveSchemaChange(BEFORE, 'Leads', draft), draft);

  it('counts a swap of two names as two changed columns, not three renames', () => {
    let draft = edit(draftOf(), 'Name', { name: 'Site2' });
    draft = edit(draft, 'Site', { name: 'Name' });
    draft = edit(draft, 'Site2', { name: 'Site' });
    expect(deriveOperations(BEFORE, 'Leads', draft)).toHaveLength(3);
    expect(summary(draft)).toEqual({ changed: 2, added: 0, removed: 0, reordered: false, retitled: false });
    expect(schemaChangePhrase(summary(draft))).toBe('2 columns changed');
  });

  it('counts several edits on one column once, plus adds, drops, order and title', () => {
    let draft = edit(draftOf(), 'Amount', { name: 'Total', precision: '0', defaultValue: '5' });
    draft = { ...draft, title: 'Deals', columns: [newDraftColumn('New', 'text'), ...draft.columns.filter((c) => c.name !== 'Done')] };
    expect(summary(draft)).toEqual({ changed: 1, added: 1, removed: 1, reordered: true, retitled: true });
    expect(schemaChangePhrase(summary(draft))).toBe('1 column changed, 1 column added, 1 column removed, new column order, new title');
  });

  it('a reorder alone, and nothing at all', () => {
    const draft = { ...draftOf(), columns: [...draftOf().columns].reverse() };
    expect(schemaChangePhrase(summary(draft))).toBe('New column order');
    expect(schemaChangePhrase(summary(draftOf()))).toBe('');
  });
});

describe('validateDraft: refused edits', () => {
  const ok = (draft: SchemaDraft, tableEmpty = false) => validateDraft(BEFORE, draft, { tableEmpty });

  it('accepts the loaded schema', () => {
    expect(ok(draftOf()).count).toBe(0);
  });

  it('refuses a type change on an existing column', () => {
    const errors = ok(edit(draftOf(), 'Name', { type: 'number' }));
    expect(errors.columns.c_00000001.type).toBe("Type can't be changed after creation.");
  });

  it('refuses multi -> single on an existing column', () => {
    const before = [col('c_1', 'Tags', { type: 'enum', options: ['a'], multiple: true })];
    const errors = validateDraft(before, edit(draftFromSchema('T', before), 'Tags', { multiple: false }), { tableEmpty: false });
    expect(errors.columns.c_1.multiple).toMatch(/can't go back/);
  });

  it('refuses dropping the last column', () => {
    expect(ok({ ...draftOf(), columns: [] }).form).toBe('A table needs at least one column.');
  });

  it('refuses removing the last option', () => {
    const errors = ok(edit(draftOf(), 'Status', { options: [], defaultOptionIds: [] }));
    expect(errors.columns.c_00000003.options).toBe('Add at least one option.');
  });

  it("refuses removing an option the default still uses", () => {
    const d = draftOf();
    const s = find(d, 'Status');
    const errors = ok(edit(d, 'Status', { options: s.options.slice(1) }));
    expect(errors.columns.c_00000003.default).toBe('The default uses a removed option.');
  });

  it('name rules: empty, too long, edge whitespace, control chars, reserved, duplicate (any case)', () => {
    const name = (value: string) => ok(edit(draftOf(), 'Name', { name: value })).columns.c_00000001?.name;
    expect(name('')).toBe('Enter a name.');
    expect(name('x'.repeat(81))).toMatch(/80/);
    expect(name(' Name')).toMatch(/spaces/);
    expect(name('Na​me')).toMatch(/invisible/);
    expect(name('_ID')).toMatch(/reserved/);
    expect(name('amount')).toMatch(/Another column/);
    // Python casefold: 'ß' folds to 'ss', so these collide on the server too.
    expect(ok(edit(edit(draftOf(), 'Name', { name: 'Straße' }), 'Site', { name: 'STRASSE' })).columns.c_00000005?.name)
      .toMatch(/Another column/);
    expect(name('x'.repeat(80))).toBeUndefined();
  });

  it('option rules: empty, too long, edge whitespace, duplicate (any case), too many', () => {
    const withOptions = (values: string[]) => {
      return ok(edit(draftOf(), 'Status', {
        options: values.map((value, i) => ({ id: `o${i}`, original: null, value })),
        defaultOptionIds: [],
      })).columns.c_00000003;
    };
    expect(withOptions(['a', ''])?.optionErrors.o1).toBe('Enter the option.');
    expect(withOptions(['a', 'b'.repeat(101)])?.optionErrors.o1).toMatch(/100/);
    expect(withOptions(['a ', 'b'])?.optionErrors.o0).toMatch(/spaces/);
    expect(withOptions(['Open', 'open'])?.optionErrors.o1).toMatch(/twice/);
    expect(withOptions(['Maß', 'MASS'])?.optionErrors.o1).toMatch(/twice/);
    expect(withOptions(Array.from({ length: 201 }, (_, i) => `o${i}`))?.options).toMatch(/200/);
    expect(withOptions(['a', 'b'])).toBeUndefined();
  });

  it('unique only on text / number / url / date; precision number-only 0..10; multiple enum-only', () => {
    expect(ok(edit(draftOf(), 'Done', { unique: true })).columns.c_00000004.unique).toMatch(/Only text/);
    const due = { ...newDraftColumn('Due', 'date'), unique: true };
    expect(ok({ ...draftOf(), columns: [...draftOf().columns, due] }).count).toBe(0);
    expect(ok({ ...draftOf(), columns: [...draftOf().columns, { ...due, precision: '2' }] }).columns[due.id].precision)
      .toMatch(/Only number/);
    expect(ok(edit(draftOf(), 'Name', { precision: '2' })).columns.c_00000001.precision).toMatch(/Only number/);
    expect(ok(edit(draftOf(), 'Amount', { precision: '11' })).columns.c_00000002.precision).toMatch(/0 to 10/);
    expect(ok(edit(draftOf(), 'Amount', { precision: '1.5' })).columns.c_00000002.precision).toMatch(/0 to 10/);
    expect(ok(edit(draftOf(), 'Name', { multiple: true })).columns.c_00000001.multiple).toMatch(/Only choice/);
  });

  it('defaults must be valid for the type', () => {
    expect(ok(edit(draftOf(), 'Amount', { defaultValue: '1,5' })).columns.c_00000002.default).toMatch(/plain number/);
    expect(ok(edit(draftOf(), 'Amount', { defaultValue: '1e3' })).count).toBe(0);
    expect(ok(edit(draftOf(), 'Site', { defaultValue: 'example.com' })).columns.c_00000005.default).toMatch(/http/);
    expect(ok(edit(draftOf(), 'Site', { defaultValue: 'https://example.com' })).count).toBe(0);
    const due = newDraftColumn('Due', 'date');
    const withDue = (defaultValue: string) => ({ ...draftOf(), columns: [...draftOf().columns, { ...due, defaultValue }] });
    expect(ok(withDue('2026-02-30')).columns[due.id].default).toMatch(/YYYY-MM-DD/);
    expect(ok(withDue('16/10/2026')).columns[due.id].default).toMatch(/YYYY-MM-DD/);
    expect(ok(withDue(' 2026-10-16 ')).count).toBe(0);
    expect(ok(withDue('')).count).toBe(0);
  });

  it('a date column default is sent trimmed as YYYY-MM-DD, with unique', () => {
    const due = { ...newDraftColumn('Due', 'date'), unique: true, defaultValue: ' 2026-10-16 ' };
    const ops = deriveOperations(BEFORE, 'Leads', { ...draftOf(), columns: [...draftOf().columns, due] });
    expect(ops).toEqual([
      { op: 'add_column', column: { name: 'Due', type: 'date', unique: true, default: '2026-10-16' }, after: 'Site' },
    ]);
  });

  it('a new required column needs a default on a table with rows, not on an empty one', () => {
    const added = { ...newDraftColumn('Owner', 'text'), required: true };
    const draft = { ...draftOf(), columns: [...draftOf().columns, added] };
    expect(ok(draft).columns[added.id].default).toMatch(/needs a default/);
    expect(ok(draft, true).count).toBe(0);
    expect(ok({ ...draft, columns: [...draftOf().columns, { ...added, defaultValue: 'me' }] }).count).toBe(0);
    // Making an EXISTING column required is the server's check (rows_violate).
    expect(ok(edit(draftOf(), 'Name', { required: true })).count).toBe(0);
  });

  it('description at most 500 characters; title 1..200 when checked', () => {
    expect(ok(edit(draftOf(), 'Name', { description: 'x'.repeat(501) })).columns.c_00000001.description).toMatch(/500/);
    expect(validateDraft([], { ...emptyDraft(), title: ' ' }, { tableEmpty: true, checkTitle: true }).title).toBe('Enter a title.');
    expect(validateDraft([], { ...emptyDraft(), title: 'x'.repeat(201) }, { tableEmpty: true, checkTitle: true }).title).toMatch(/200/);
    expect(validateDraft([], { ...emptyDraft(), title: 'Leads' }, { tableEmpty: true, checkTitle: true }).count).toBe(0);
  });
});

describe('draftToCreateRequest', () => {
  it('builds the POST /tables body', () => {
    const draft: SchemaDraft = {
      title: ' Leads ',
      columns: [
        newDraftColumn('Name', 'text'),
        { ...newDraftColumn('Stage', 'enum'), options: [{ id: 'a', original: null, value: 'new' }], defaultOptionIds: ['a'] },
      ],
    };
    expect(draftToCreateRequest(draft, { projectId: 'p1', description: ' Sales ' })).toEqual({
      title: 'Leads',
      description: 'Sales',
      project_id: 'p1',
      columns: [
        { name: 'Name', type: 'text' },
        { name: 'Stage', type: 'enum', options: ['new'], default: 'new' },
      ],
    });
    expect(draftToCreateRequest(emptyDraft())).toEqual({ title: '', columns: [{ name: 'Name', type: 'text' }] });
  });
});

describe('rebaseDraft', () => {
  it('re-applies edits over a newer schema and drops those whose column is gone', () => {
    let draft = edit(draftOf(), 'Name', { name: 'Who' });
    draft = edit(draft, 'Site', { description: 'Homepage' });
    const added = newDraftColumn('Extra', 'text');
    draft = { ...draft, columns: [...draft.columns, added] };
    // Meanwhile: Site dropped, a column "Score" added, Status got an option.
    const newer = [
      ...BEFORE.filter((c) => c.name !== 'Site').map((c) =>
        c.name === 'Status' ? { ...c, options: ['open', 'won', 'lost', 'paused'] } : c,
      ),
      col('c_00000009', 'Score', { type: 'number' }),
    ];
    const { draft: rebased, dropped } = rebaseDraft(BEFORE, draft, 'Leads', newer, 'Leads');
    expect(rebased.columns.map((c) => c.name)).toEqual(['Who', 'Amount', 'Status', 'Done', 'Score', 'Extra']);
    expect(find(rebased, 'Status').options.map((o) => o.value)).toEqual(['open', 'won', 'lost', 'paused']);
    expect(dropped).toEqual(['Column Site was removed meanwhile; your edits to it were dropped.']);
    expect(deriveOperations(newer, 'Leads', rebased)).toEqual([
      { op: 'rename_column', name: 'Name', new_name: 'Who' },
      { op: 'add_column', column: { name: 'Extra', type: 'text' }, after: 'Score' },
    ]);
  });

  it('keeps option renames and removals by original spelling', () => {
    const d = draftOf();
    const s = find(d, 'Status');
    const draft = edit(d, 'Status', {
      options: s.options.filter((o) => o.value !== 'lost').map((o) => (o.value === 'won' ? { ...o, value: 'Won' } : o)),
    });
    const newer = BEFORE.map((c) => (c.name === 'Status' ? { ...c, options: ['new', 'open', 'won', 'lost'] } : c));
    const { draft: rebased } = rebaseDraft(BEFORE, draft, 'Leads', newer, 'Leads');
    expect(deriveOperations(newer, 'Leads', rebased)).toEqual([
      { op: 'remove_enum_option', name: 'Status', option: 'lost' },
      { op: 'rename_enum_option', name: 'Status', option: 'won', new_option: 'Won' },
    ]);
  });
});
