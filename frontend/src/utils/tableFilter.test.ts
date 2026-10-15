// tableFilter: builder state -> filter DSL for every op of every column type,
// validation (incomplete / invalid values block Apply), the inverse mapping
// with non-representable filters reported as custom, server error paths,
// and the ?filter= / ?sort= URL round trip with special characters.
import { describe, expect, it } from 'vitest';
import type { FilterNode, FilterOp, TableColumn } from '../api/types';
import {
  FILTER_MAX_CONDITIONS,
  compareDecimals,
  conditionError,
  conditionLabel,
  conditionsError,
  conditionsToFilter,
  conditionToLeaf,
  decimalDigitCount,
  emptyValue,
  filterColumnNames,
  filterErrorTarget,
  filterToConditions,
  filterToSearchParam,
  isConditionComplete,
  isDecimalString,
  isHttpUrl,
  missingFilterColumns,
  opsForColumn,
  searchParamToFilter,
  searchParamToSort,
  sortToSearchParam,
  splitLines,
  valueKind,
} from './tableFilter';
import type { FilterCondition, FilterValue } from './tableFilter';
import { parseTablesRoute, tableViewerPath, tableViewerSearch } from './tablesRoute';

function col(name: string, type: TableColumn['type'], extra: Partial<TableColumn> = {}): TableColumn {
  return {
    key: `c_${name.toLowerCase()}`, name, type, required: false, unique: false, default: null,
    options: null, multiple: false, precision: null, description: '', ...extra,
  };
}

const SCHEMA: TableColumn[] = [
  col('Name', 'text'),
  col('Site', 'url'),
  col('Revenue', 'number', { precision: 2 }),
  col('Active', 'boolean'),
  col('Status', 'enum', { options: ['Lead', 'Customer', 'Churned'] }),
  col('Tags', 'enum', { options: ['a', 'b', 'c'], multiple: true }),
];

const byName = (name: string) => SCHEMA.find((c) => c.name === name)!;

let n = 0;
function cond(column: string, op: FilterOp, value: FilterValue = null): FilterCondition {
  n += 1;
  return { id: `t${n}`, column, op, value };
}

describe('opsForColumn', () => {
  it('offers the server ops per type, in menu order', () => {
    const ops = (name: string) => opsForColumn(byName(name)).map((c) => c.op);
    expect(ops('Name')).toEqual(['eq', 'ne', 'contains', 'starts_with', 'ends_with', 'in', 'is_null', 'not_null']);
    expect(ops('Site')).toEqual(ops('Name'));
    expect(ops('Revenue')).toEqual(['eq', 'ne', 'lt', 'lte', 'gt', 'gte', 'between', 'in', 'is_null', 'not_null']);
    expect(ops('Active')).toEqual(['eq', 'ne', 'in', 'is_null', 'not_null']);
    expect(ops('Status')).toEqual(['eq', 'ne', 'in', 'is_null', 'not_null']);
    expect(ops('Tags')).toEqual(['contains', 'contains_any', 'contains_all', 'eq', 'ne', 'is_empty', 'not_null']);
  });

  it('labels ops for people', () => {
    const labels = (name: string) => opsForColumn(byName(name)).map((c) => c.label);
    expect(labels('Revenue')).toEqual(['=', '≠', '<', '≤', '>', '≥', 'between', 'is any of', 'is empty', 'is not empty']);
    expect(labels('Tags')).toEqual(['has', 'has any of', 'has all of', 'is exactly', 'is not exactly', 'is empty', 'is not empty']);
  });
});

// Every op of every type: builder condition -> DSL leaf.
const LEAF_CASES: [string, FilterOp, FilterValue, FilterNode][] = [
  // text
  ['Name', 'eq', 'Ada', { column: 'Name', op: 'eq', value: 'Ada' }],
  ['Name', 'ne', 'Ada', { column: 'Name', op: 'ne', value: 'Ada' }],
  ['Name', 'contains', ' lov ', { column: 'Name', op: 'contains', value: ' lov ' }],
  ['Name', 'starts_with', 'A', { column: 'Name', op: 'starts_with', value: 'A' }],
  ['Name', 'ends_with', 'e', { column: 'Name', op: 'ends_with', value: 'e' }],
  // text values go as typed (not trimmed), repeats dropped
  ['Name', 'in', [' Ada ', 'Bob', 'Ada', 'Bob'], { column: 'Name', op: 'in', value: [' Ada ', 'Bob', 'Ada'] }],
  ['Name', 'is_null', null, { column: 'Name', op: 'is_null' }],
  ['Name', 'not_null', null, { column: 'Name', op: 'not_null' }],
  // url
  ['Site', 'eq', 'https://x.io', { column: 'Site', op: 'eq', value: 'https://x.io' }],
  ['Site', 'ne', 'https://x.io', { column: 'Site', op: 'ne', value: 'https://x.io' }],
  ['Site', 'contains', 'x.io/a?b=1&c', { column: 'Site', op: 'contains', value: 'x.io/a?b=1&c' }],
  ['Site', 'starts_with', 'https://', { column: 'Site', op: 'starts_with', value: 'https://' }],
  ['Site', 'ends_with', '.org', { column: 'Site', op: 'ends_with', value: '.org' }],
  ['Site', 'in', ['https://a.io', 'https://b.io'], { column: 'Site', op: 'in', value: ['https://a.io', 'https://b.io'] }],
  ['Site', 'is_null', null, { column: 'Site', op: 'is_null' }],
  ['Site', 'not_null', null, { column: 'Site', op: 'not_null' }],
  // number: decimal strings, trimmed, not rounded to the column precision
  ['Revenue', 'eq', ' 10.055 ', { column: 'Revenue', op: 'eq', value: '10.055' }],
  ['Revenue', 'ne', '-3', { column: 'Revenue', op: 'ne', value: '-3' }],
  ['Revenue', 'lt', '1e3', { column: 'Revenue', op: 'lt', value: '1e3' }],
  ['Revenue', 'lte', '.5', { column: 'Revenue', op: 'lte', value: '.5' }],
  ['Revenue', 'gt', '0', { column: 'Revenue', op: 'gt', value: '0' }],
  ['Revenue', 'gte', '+7.25', { column: 'Revenue', op: 'gte', value: '+7.25' }],
  ['Revenue', 'between', [' 10', '20 '], { column: 'Revenue', op: 'between', value: ['10', '20'] }],
  ['Revenue', 'between', ['5', '5'], { column: 'Revenue', op: 'between', value: ['5', '5'] }],
  ['Revenue', 'in', ['1', ' 2.50', '1'], { column: 'Revenue', op: 'in', value: ['1', '2.50'] }],
  ['Revenue', 'is_null', null, { column: 'Revenue', op: 'is_null' }],
  ['Revenue', 'not_null', null, { column: 'Revenue', op: 'not_null' }],
  // boolean: JSON booleans
  ['Active', 'eq', true, { column: 'Active', op: 'eq', value: true }],
  ['Active', 'ne', false, { column: 'Active', op: 'ne', value: false }],
  ['Active', 'in', [false, true, false], { column: 'Active', op: 'in', value: [false, true] }],
  ['Active', 'is_null', null, { column: 'Active', op: 'is_null' }],
  ['Active', 'not_null', null, { column: 'Active', op: 'not_null' }],
  // single enum: options exactly, lists in schema order
  ['Status', 'eq', 'Customer', { column: 'Status', op: 'eq', value: 'Customer' }],
  ['Status', 'ne', 'Lead', { column: 'Status', op: 'ne', value: 'Lead' }],
  ['Status', 'in', ['Churned', 'Lead'], { column: 'Status', op: 'in', value: ['Lead', 'Churned'] }],
  ['Status', 'is_null', null, { column: 'Status', op: 'is_null' }],
  ['Status', 'not_null', null, { column: 'Status', op: 'not_null' }],
  // multi enum
  ['Tags', 'contains', 'b', { column: 'Tags', op: 'contains', value: 'b' }],
  ['Tags', 'contains_any', ['c', 'a'], { column: 'Tags', op: 'contains_any', value: ['a', 'c'] }],
  ['Tags', 'contains_all', ['b', 'a'], { column: 'Tags', op: 'contains_all', value: ['a', 'b'] }],
  ['Tags', 'eq', ['c', 'a'], { column: 'Tags', op: 'eq', value: ['a', 'c'] }],
  ['Tags', 'ne', ['b'], { column: 'Tags', op: 'ne', value: ['b'] }],
  ['Tags', 'is_empty', null, { column: 'Tags', op: 'is_empty' }],
  ['Tags', 'not_null', null, { column: 'Tags', op: 'not_null' }],
];

describe('conditionToLeaf / conditionsToFilter', () => {
  it.each(LEAF_CASES)('%s %s %j', (column, op, value, leaf) => {
    const condition = cond(column, op, value);
    expect(conditionError(condition, SCHEMA)).toBeNull();
    expect(isConditionComplete(condition, SCHEMA)).toBe(true);
    expect(conditionToLeaf(condition, SCHEMA)).toEqual(leaf);
    // One condition: the bare leaf, no `and` wrapper.
    expect(conditionsToFilter([condition], SCHEMA)).toEqual(leaf);
    // Exact JSON: nullary ops have no value key, booleans are not strings.
    expect(JSON.stringify(conditionsToFilter([condition], SCHEMA))).toBe(JSON.stringify(leaf));
  });

  it('covers every offered op of every column', () => {
    for (const column of SCHEMA) {
      for (const { op } of opsForColumn(column)) {
        expect(LEAF_CASES.some(([name, o]) => name === column.name && o === op)).toBe(true);
      }
    }
  });

  it('emits null for no conditions and `and` for several', () => {
    expect(conditionsToFilter([], SCHEMA)).toBeNull();
    expect(
      conditionsToFilter([cond('Status', 'eq', 'Lead'), cond('Revenue', 'between', ['1', '2'])], SCHEMA),
    ).toEqual({
      and: [
        { column: 'Status', op: 'eq', value: 'Lead' },
        { column: 'Revenue', op: 'between', value: ['1', '2'] },
      ],
    });
  });

  it('uses the schema spelling of a column name', () => {
    expect(conditionToLeaf(cond('status', 'eq', 'Lead'), SCHEMA)).toEqual({ column: 'Status', op: 'eq', value: 'Lead' });
  });
});

describe('validation', () => {
  const INVALID: [string, FilterOp, FilterValue, string][] = [
    ['Name', 'eq', '', 'Enter a value'],
    ['Name', 'contains', null, 'Enter a value'],
    ['Site', 'starts_with', '', 'Enter a value'],
    ['Name', 'in', [], 'Enter at least one value'],
    ['Revenue', 'eq', '', 'Enter a number'],
    ['Revenue', 'eq', '1,234', 'Enter a number'],
    ['Revenue', 'gt', '12 USD', 'Enter a number'],
    ['Revenue', 'lt', 'NaN', 'Enter a number'],
    ['Revenue', 'lte', '1e', 'Enter a number'],
    ['Revenue', 'between', ['10', ''], 'Enter both bounds'],
    ['Revenue', 'between', ['', '3'], 'Enter both bounds'],
    ['Revenue', 'between', ['20', '10'], 'The first bound must not be larger'],
    ['Revenue', 'between', ['1', 'x'], 'Enter numbers'],
    ['Revenue', 'in', ['1', '2,5'], 'Enter numbers only'],
    ['Revenue', 'in', ['1,234'], 'Enter numbers only'],
    // 38 significant digits at most (the server's canonical number)
    ['Revenue', 'eq', '1'.repeat(39), 'Use at most 38 digits'],
    ['Revenue', 'gt', '1e38', 'Use at most 38 digits'],
    ['Revenue', 'lt', '1e-39', 'Use at most 38 digits'],
    ['Revenue', 'between', ['0', '1'.repeat(39)], 'Use at most 38 digits'],
    ['Revenue', 'in', ['1', `0.${'1'.repeat(39)}`], 'Use at most 38 digits'],
    // exact decimal comparison: Number() would call these equal or ordered
    ['Revenue', 'between', ['0.30000000000000001', '0.3'], 'The first bound must not be larger'],
    ['Revenue', 'between', ['12345678901234567891', '12345678901234567890'], 'The first bound must not be larger'],
    ['Revenue', 'between', ['1e2', '99.9'], 'The first bound must not be larger'],
    ['Revenue', 'between', ['-1', '-2'], 'The first bound must not be larger'],
    // url eq / ne / in operands are http(s) URLs with a host
    ['Site', 'eq', 'x.io', 'Enter an http(s) URL'],
    ['Site', 'ne', 'ftp://x.io', 'Enter an http(s) URL'],
    ['Site', 'eq', 'https://', 'Enter an http(s) URL'],
    ['Site', 'eq', 'http:x.io', 'Enter an http(s) URL'],
    ['Site', 'eq', 'https://a b.io', 'Enter an http(s) URL'],
    ['Site', 'eq', 'https://evil.io\\@good.io', 'Enter an http(s) URL'],
    ['Site', 'eq', 'https://x.io:99999', 'Enter an http(s) URL'],
    ['Site', 'eq', `https://x.io/${'a'.repeat(2048)}`, 'Enter an http(s) URL'],
    ['Site', 'in', ['https://a.io', 'b.io'], 'Enter an http(s) URL'],
    ['Active', 'eq', null, 'Choose true or false'],
    ['Active', 'eq', 'true', 'Choose true or false'],
    ['Active', 'in', [], 'Choose at least one'],
    ['Status', 'eq', '', 'Choose an option'],
    ['Status', 'eq', 'customer', 'Choose an option'],
    ['Status', 'in', [], 'Choose at least one option'],
    ['Tags', 'contains', 'z', 'Choose an option'],
    ['Tags', 'contains_any', [], 'Choose at least one option'],
    ['Tags', 'contains_all', ['a', 'z'], 'Unknown option'],
    ['Tags', 'eq', [], 'Choose at least one option'],
    ['Nope', 'eq', 'x', 'Choose a column'],
    ['Active', 'contains', 'x', 'Choose a condition'],
    ['Tags', 'in', [['a']] as unknown as string[], 'Choose a condition'],
  ];

  it.each(INVALID)('%s %s %j -> %s', (column, op, value, message) => {
    const condition = cond(column, op, value);
    expect(conditionError(condition, SCHEMA)).toBe(message);
    expect(isConditionComplete(condition, SCHEMA)).toBe(false);
  });

  it('caps the condition count and list sizes', () => {
    const many = Array.from({ length: FILTER_MAX_CONDITIONS + 1 }, () => cond('Name', 'not_null'));
    expect(conditionsError(many, SCHEMA)).toBe('At most 20 filters');
    expect(conditionsError(many.slice(1), SCHEMA)).toBeNull();
    expect(conditionsError([cond('Name', 'not_null'), cond('Name', 'eq', '')], SCHEMA)).toBe('Enter a value');
    const hundredOne = Array.from({ length: 101 }, (_, i) => String(i));
    expect(conditionError(cond('Revenue', 'in', hundredOne), SCHEMA)).toBe('At most 100 values');
  });

  it('caps list ops at 100 options; only a multi-enum exact set takes 200', () => {
    const options = Array.from({ length: 200 }, (_, i) => `o${i}`);
    const schema = [
      col('Many', 'enum', { options, multiple: true }),
      col('One', 'enum', { options }),
    ];
    const check = (column: string, op: FilterOp, count: number) =>
      conditionError(cond(column, op, options.slice(0, count)), schema);
    for (const op of ['contains_any', 'contains_all'] as FilterOp[]) {
      expect(check('Many', op, 100)).toBeNull();
      expect(check('Many', op, 101)).toBe('At most 100 options');
    }
    expect(check('One', 'in', 100)).toBeNull();
    expect(check('One', 'in', 101)).toBe('At most 100 options');
    for (const op of ['eq', 'ne'] as FilterOp[]) {
      expect(check('Many', op, 101)).toBeNull();
      expect(check('Many', op, 200)).toBeNull();
    }
  });

  it('url substring ops take plain text', () => {
    for (const op of ['contains', 'starts_with', 'ends_with'] as FilterOp[]) {
      expect(conditionError(cond('Site', op, 'x.io/a b'), SCHEMA)).toBeNull();
    }
    expect(conditionError(cond('Site', 'eq', ' https://x.io/a?b=1#c '), SCHEMA)).toBeNull();
  });

  it('isHttpUrl follows the server URL rule', () => {
    for (const ok of ['http://a', 'HTTPS://A.io/x?y#z', 'https://u:p@x.io:8080/', 'http://[::1]:80/', 'http://a:', ' https://x.io ']) {
      expect(isHttpUrl(ok)).toBe(true);
    }
    for (const bad of [
      '', 'x.io', 'ftp://x.io', 'http://', 'http://:80', 'http://u@:80', 'http:x.io', 'http://a b', 'http://a\tb',
      'http://a\\b', 'http://a:x', 'http://a:b:c', 'http://a:65536', 'http://[::1', 'http://x.io/​',
      'http://x.io/\u0085', `http://x.io/${'a'.repeat(2040)}`,
    ]) {
      expect(isHttpUrl(bad)).toBe(false);
    }
  });

  it('compareDecimals compares exactly', () => {
    expect(compareDecimals('1', '2')).toBe(-1);
    expect(compareDecimals('2', '1')).toBe(1);
    expect(compareDecimals('1e2', '100.000')).toBe(0);
    expect(compareDecimals('0', '-0.0e5')).toBe(0);
    expect(compareDecimals('-5', '0')).toBe(-1);
    expect(compareDecimals('-1.5', '-1.25')).toBe(-1);
    expect(compareDecimals('0.30000000000000001', '0.3')).toBe(1);
    expect(compareDecimals('.5', '0.50')).toBe(0);
    expect(compareDecimals('+7', '7')).toBe(0);
    expect(compareDecimals('1e999999999999', '1')).toBe(1);
    expect(compareDecimals('1e-999999999999', '0')).toBe(1);
    expect(compareDecimals('x', '1')).toBeNull();
  });

  it('decimalDigitCount counts the canonical plain digits', () => {
    expect(decimalDigitCount('0')).toBe(0);
    expect(decimalDigitCount('0e99999')).toBe(0);
    expect(decimalDigitCount('120')).toBe(3);
    expect(decimalDigitCount('1.50')).toBe(2);
    expect(decimalDigitCount('0.001')).toBe(3);
    expect(decimalDigitCount('1.5e-37')).toBe(38);
    expect(decimalDigitCount('1e37')).toBe(38);
    expect(decimalDigitCount('1e38')).toBe(39);
    expect(decimalDigitCount('nope')).toBeNull();
  });

  it('isDecimalString follows the server grammar', () => {
    for (const ok of ['0', '-1', '+2.', '.5', '1e10', ' 3.14 ', '1E-3']) expect(isDecimalString(ok)).toBe(true);
    for (const bad of ['', '1,000', '1_000', 'Infinity', '٣', '1.2.3', '$5', '1e']) expect(isDecimalString(bad)).toBe(false);
  });

  it('splitLines splits on newlines only, keeping text as typed', () => {
    expect(splitLines(' a, b \n\n  \n c ')).toEqual([' a, b ', ' c ']);
    expect(splitLines(' 1 \n3\n', true)).toEqual(['1', '3']);
    // A thousands separator is one (invalid) value, not two.
    expect(splitLines('1,234', true)).toEqual(['1,234']);
  });

  it('valueKind and emptyValue', () => {
    expect(valueKind(byName('Revenue'), 'between')).toBe('pair');
    expect(valueKind(byName('Revenue'), 'in')).toBe('lines');
    expect(valueKind(byName('Active'), 'in')).toBe('booleans');
    expect(valueKind(byName('Status'), 'in')).toBe('options');
    expect(valueKind(byName('Tags'), 'contains')).toBe('option');
    expect(valueKind(byName('Tags'), 'eq')).toBe('options');
    expect(valueKind(byName('Tags'), 'is_empty')).toBe('none');
    expect(emptyValue('pair')).toEqual(['', '']);
    expect(emptyValue('options')).toEqual([]);
    expect(emptyValue('boolean')).toBeNull();
  });
});

describe('conditionLabel', () => {
  it('reads as a sentence with column names', () => {
    const label = (column: string, op: FilterOp, value: FilterValue = null) =>
      conditionLabel(cond(column, op, value), SCHEMA);
    expect(label('Status', 'eq', 'Customer')).toBe('Status is Customer');
    expect(label('Revenue', 'between', ['10', '20'])).toBe('Revenue between 10 and 20');
    expect(label('Revenue', 'gte', '5')).toBe('Revenue ≥ 5');
    expect(label('Name', 'is_null')).toBe('Name is empty');
    expect(label('Active', 'eq', false)).toBe('Active is false');
    expect(label('Tags', 'contains_any', ['a', 'b'])).toBe('Tags has any of a, b');
  });
});

describe('filterToConditions', () => {
  const strip = (conditions: FilterCondition[]) => conditions.map(({ column, op, value }) => ({ column, op, value }));

  it.each(LEAF_CASES)('round-trips %s %s', (column, op, _value, leaf) => {
    const parsed = filterToConditions(leaf, SCHEMA);
    expect(parsed.kind).toBe('conditions');
    if (parsed.kind !== 'conditions') return;
    expect(parsed.conditions).toHaveLength(1);
    expect(conditionsToFilter(parsed.conditions, SCHEMA)).toEqual(leaf);
    expect(parsed.conditions[0].column).toBe(column);
    expect(parsed.conditions[0].op).toBe(op);
  });

  // Conditions -> DSL -> conditions -> DSL is the identity for every leaf
  // read back as a chip, so an untouched chip is re-emitted unchanged.
  it.each(LEAF_CASES)('re-emits %s %s unchanged', (column, op, value) => {
    const first = conditionsToFilter([cond(column, op, value)], SCHEMA);
    const parsed = filterToConditions(first, SCHEMA);
    expect(parsed.kind).toBe('conditions');
    if (parsed.kind !== 'conditions') return;
    const second = conditionsToFilter(parsed.conditions, SCHEMA);
    expect(JSON.stringify(second)).toBe(JSON.stringify(first));
  });

  it('re-emits a hand-made text or url leaf exactly (no trimming)', () => {
    const leaves: FilterNode[] = [
      { column: 'Name', op: 'eq', value: '  padded  ' },
      { column: 'Name', op: 'in', value: [' a', 'b ', '  c  '] },
      { column: 'Site', op: 'in', value: [' https://a.io', 'https://b.io '] },
      { column: 'Site', op: 'eq', value: ' https://a.io ' },
      { column: 'Name', op: 'contains', value: ' x ' },
    ];
    for (const leaf of leaves) {
      const parsed = filterToConditions(leaf, SCHEMA);
      expect(parsed.kind).toBe('conditions');
      if (parsed.kind !== 'conditions') continue;
      expect(conditionsToFilter(parsed.conditions, SCHEMA)).toEqual(leaf);
    }
  });

  it('reads none, a flat and, and and-of-one', () => {
    expect(filterToConditions(null, SCHEMA)).toEqual({ kind: 'none' });
    const both: FilterNode = {
      and: [
        { column: 'Status', op: 'in', value: ['Lead'] },
        { column: 'Active', op: 'eq', value: true },
      ],
    };
    const parsed = filterToConditions(both, SCHEMA);
    expect(parsed.kind === 'conditions' && strip(parsed.conditions)).toEqual([
      { column: 'Status', op: 'in', value: ['Lead'] },
      { column: 'Active', op: 'eq', value: true },
    ]);
    const one = filterToConditions({ and: [{ column: 'Name', op: 'is_null' }] }, SCHEMA);
    expect(one.kind === 'conditions' && strip(one.conditions)).toEqual([{ column: 'Name', op: 'is_null', value: null }]);
  });

  it('accepts JSON numbers for number operands as decimal strings', () => {
    const parsed = filterToConditions({ column: 'Revenue', op: 'between', value: [1, 2.5] }, SCHEMA);
    expect(parsed.kind === 'conditions' && conditionsToFilter(parsed.conditions, SCHEMA)).toEqual({
      column: 'Revenue', op: 'between', value: ['1', '2.5'],
    });
  });

  const CUSTOM: [string, unknown][] = [
    ['or', { or: [{ column: 'Name', op: 'is_null' }] }],
    ['not', { not: { column: 'Name', op: 'is_null' } }],
    ['nested and', { and: [{ and: [{ column: 'Name', op: 'is_null' }] }] }],
    ['and with an or child', { and: [{ column: 'Name', op: 'is_null' }, { or: [{ column: 'Name', op: 'is_null' }] }] }],
    ['empty and', { and: [] }],
    ['unknown column', { column: 'Gone', op: 'is_null' }],
    ['op not offered on the type', { column: 'Active', op: 'contains', value: 'x' }],
    ['multi-enum in (list of lists)', { column: 'Tags', op: 'in', value: [['a']] }],
    ['unknown op', { column: 'Name', op: 'like', value: 'x' }],
    ['string boolean', { column: 'Active', op: 'eq', value: 'true' }],
    ['between out of order', { column: 'Revenue', op: 'between', value: ['9', '1'] }],
    ['between one bound', { column: 'Revenue', op: 'between', value: ['1'] }],
    ['locale number', { column: 'Revenue', op: 'gt', value: '1,000' }],
    ['unknown option', { column: 'Status', op: 'eq', value: 'Nope' }],
    ['value on a nullary op', { column: 'Name', op: 'is_null', value: 'x' }],
    ['extra key', { column: 'Name', op: 'is_null', extra: 1 }],
    ['mixed shapes', { and: [{ column: 'Name', op: 'is_null' }], or: [] }],
    ['too many conditions', { and: Array.from({ length: 21 }, () => ({ column: 'Name', op: 'is_null' })) }],
    ['text in value spanning lines', { column: 'Name', op: 'in', value: ['a\nb'] }],
    ['whitespace-only text in value', { column: 'Name', op: 'in', value: [' '] }],
    ['url eq that is not a URL', { column: 'Site', op: 'eq', value: 'x.io' }],
    ['between out of order past float precision', { column: 'Revenue', op: 'between', value: ['0.30000000000000001', '0.3'] }],
  ];

  it.each(CUSTOM)('reports %s as custom', (_label, node) => {
    const parsed = filterToConditions(node as FilterNode, SCHEMA);
    expect(parsed.kind).toBe('custom');
    expect(parsed.kind === 'custom' && parsed.reason).toBeTruthy();
  });
});

describe('column references', () => {
  it('walks and / or / not and lists missing columns once', () => {
    const node: FilterNode = {
      and: [{ column: 'A', op: 'is_null' }, { or: [{ not: { column: 'Name', op: 'is_null' } }, { column: 'A', op: 'is_null' }] }],
    };
    expect(filterColumnNames(node)).toEqual(['A', 'Name', 'A']);
    expect(missingFilterColumns(node, SCHEMA)).toEqual(['A']);
    expect(missingFilterColumns({ column: 'name', op: 'is_null' }, SCHEMA)).toEqual([]);
    expect(missingFilterColumns(null, SCHEMA)).toEqual([]);
  });
});

describe('filterErrorTarget', () => {
  it.each([
    ['and[1].value', 3, 1],
    ['and[0].column', 3, 0],
    ['and[2].value[1]', 3, 2],
    ['and[5].op', 3, -1],
    ['and', 3, -1],
    ['', 3, -1],
    ['value', 1, 0],
    ['value[0]', 1, 0],
    ['column', 1, 0],
    ['', 1, -1],
    ['filter', 1, -1],
    ['or[0].value', 0, -1],
    ['sort[0].direction', 2, null],
    ['limit', 2, null],
    ['offset', 2, null],
    ['columns[0]', 2, null],
  ] as [string, number, number | null][])('%s with %d chips -> %s', (path, count, target) => {
    expect(filterErrorTarget(path, count)).toBe(target);
  });

  it('null path is no target', () => {
    expect(filterErrorTarget(null, 2)).toBeNull();
  });
});

describe('URL params', () => {
  const TRICKY: FilterNode = {
    and: [
      { column: 'Name', op: 'contains', value: 'a+b & c #1 50% "quoted" \'single\' é漢字🙂 =?/' },
      { column: 'Revenue', op: 'between', value: ['-1.5', '2e3'] },
      { column: 'Active', op: 'eq', value: false },
      { column: 'Tags', op: 'contains_all', value: ['a', 'b'] },
      { column: 'Site', op: 'is_null' },
    ],
  };
  const SORT = [{ column: 'Revenue', direction: 'desc' as const }];

  it('round-trips state -> URL -> state with special characters', () => {
    const parsed = filterToConditions(TRICKY, SCHEMA);
    expect(parsed.kind).toBe('conditions');
    if (parsed.kind !== 'conditions') return;
    const filterParam = filterToSearchParam(conditionsToFilter(parsed.conditions, SCHEMA));
    const path = tableViewerPath('t1', { filter: filterParam, sort: sortToSearchParam(SORT) });
    const [pathname, search] = path.split('?');
    const route = parseTablesRoute(pathname, `?${search}`);
    expect(route?.kind).toBe('viewer');
    if (route?.kind !== 'viewer') return;
    const back = searchParamToFilter(route.filter);
    expect(back).toEqual(TRICKY);
    const again = filterToConditions(back, SCHEMA);
    expect(again.kind === 'conditions' && again.conditions.map(({ column, op, value }) => ({ column, op, value }))).toEqual(
      parsed.conditions.map(({ column, op, value }) => ({ column, op, value })),
    );
    expect(searchParamToSort(route.sort)).toEqual(SORT);
  });

  it('tableViewerSearch sets / removes the params, keeps others, and survives decoding', () => {
    const filter = filterToSearchParam(TRICKY)!;
    const search = tableViewerSearch('?keep=a%2Bb&sort=old', { filter, sort: null });
    const route = parseTablesRoute('/tables/t1', search);
    expect(route).toEqual({ kind: 'viewer', tableId: 't1', filter, sort: null });
    expect(new URLSearchParams(search).get('keep')).toBe('a+b');
    expect(search).not.toMatch(/[ #"{}]/);
    expect(tableViewerSearch(search, { filter: null, sort: null })).toBe('?keep=a%2Bb');
    expect(tableViewerSearch('', { filter: null, sort: null })).toBe('');
  });

  it('filterToSearchParam / searchParamToFilter', () => {
    expect(filterToSearchParam(null)).toBeNull();
    expect(filterToSearchParam({ column: 'Name', op: 'is_null' })).toBe('{"column":"Name","op":"is_null"}');
    expect(searchParamToFilter(null)).toBeNull();
    expect(searchParamToFilter('')).toBeNull();
    expect(searchParamToFilter('{oops')).toBeNull();
    expect(searchParamToFilter('[1]')).toBeNull();
    expect(searchParamToFilter('"x"')).toBeNull();
  });

  it('sortToSearchParam / searchParamToSort', () => {
    expect(sortToSearchParam(null)).toBeNull();
    expect(sortToSearchParam([])).toBeNull();
    expect(sortToSearchParam(SORT)).toBe('[{"column":"Revenue","direction":"desc"}]');
    expect(searchParamToSort('{"column":"A","direction":"asc"}')).toEqual([{ column: 'A', direction: 'asc' }]);
    expect(searchParamToSort('[{"column":"A","direction":"up"}]')).toBeNull();
    expect(searchParamToSort('[{"column":"","direction":"asc"}]')).toBeNull();
    expect(searchParamToSort('[]')).toBeNull();
    expect(
      searchParamToSort(JSON.stringify(Array.from({ length: 4 }, () => ({ column: 'A', direction: 'asc' })))),
    ).toBeNull();
    expect(searchParamToSort('nope')).toBeNull();
  });

  it('searchParamToSort keeps every key, defaults the direction, refuses a repeat', () => {
    expect(
      searchParamToSort('[{"column":"A","direction":"desc"},{"column":"B"},{"column":"C","direction":null}]'),
    ).toEqual([
      { column: 'A', direction: 'desc' },
      { column: 'B', direction: 'asc' },
      { column: 'C', direction: 'asc' },
    ]);
    expect(searchParamToSort('[{"column":"A"},{"column":"A","direction":"desc"}]')).toBeNull();
  });
});
