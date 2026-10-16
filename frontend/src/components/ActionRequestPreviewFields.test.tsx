// ActionRequestPreviewFields: the Quest Tables field types dispatch to their
// previews, stacked full width under the shared `table-preview-field` class
// with their label line ("Columns (n)" for the column table, "key: value"
// for the others), in both class namespaces; a missing payload falls back
// to the plain value.
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import type { PreviewField } from '../api/types';
import { ActionRequestPreviewFields, tableFieldLabel } from './ActionRequestPreviewFields';

const COLUMNS_FIELD: PreviewField = {
  key: 'Columns',
  value: '1 column: Status (enum)',
  type: 'table_columns',
  columns: [{
    name: 'Status', type: 'enum', required: false, unique: false, default: 'Open',
    options: ['Open', 'Done'], multiple: false, precision: null, description: '',
  }],
};

const SCHEMA_FIELD: PreviewField = {
  key: 'Changes',
  value: '1 operation: Renamed column State to Status',
  type: 'table_schema_diff',
  schema_diff: {
    columns: [{
      status: 'renamed', moved: false,
      before: {
        name: 'State', type: 'text', required: false, unique: false, default: null,
        options: null, multiple: false, precision: null, description: '',
      },
      after: {
        name: 'Status', type: 'text', required: false, unique: false, default: null,
        options: null, multiple: false, precision: null, description: '',
      },
      changes: { name: { before: 'State', after: 'Status' } },
      options_added: [], options_removed: [], cells_rewritten: 0, options_renamed: [],
    }],
    order_changed: false, before_order: ['State'], after_order: ['Status'], changed: true,
    cells_rewritten_total: 0,
  },
};

const ROWS_FIELD: PreviewField = {
  key: 'Rows',
  value: 'Insert 1 rows',
  type: 'table_rows_diff',
  rows_diff: {
    columns: ['Name'],
    rows: [{ _id: null, status: 'added', before: null, after: { Name: 'Lin' }, changed_columns: [] }],
    total: 1,
    truncated: false,
  },
};

function renderFields(fields: PreviewField[], classPrefix: 'action-request-preview' | 'request-preview' = 'action-request-preview') {
  return render(
    <ActionRequestPreviewFields
      previewFields={fields}
      params={{}}
      conversationId="conv1"
      classPrefix={classPrefix}
    />,
  );
}

describe('ActionRequestPreviewFields (Quest Tables types)', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders table_columns as the column table, labelled with the count', () => {
    const { container } = renderFields([COLUMNS_FIELD]);
    const field = container.querySelector('.action-request-preview-field.table-preview-field');
    expect(field).toBeTruthy();
    // Not the server's value: it repeats the table in raw type names.
    expect(field?.querySelector('.action-request-preview-key')?.textContent).toBe('Columns (1)');
    expect(field?.textContent).not.toContain('(enum)');
    expect(field?.querySelector('.table-columns-preview .table-columns-table')).toBeTruthy();
    expect(field?.querySelector('.table-schema-diff, .table-rows-diff')).toBeNull();
  });

  it('renders table_schema_diff as the schema diff', () => {
    const { container } = renderFields([SCHEMA_FIELD], 'request-preview');
    const field = container.querySelector('.request-preview-field.table-preview-field');
    expect(field).toBeTruthy();
    expect(field?.querySelector('.request-preview-key')?.textContent)
      .toBe('Changes: 1 operation: Renamed column State to Status');
    expect(field?.querySelector('.table-schema-diff .table-schema-diff-table')).toBeTruthy();
    expect(field?.querySelector('.table-columns-preview, .table-rows-diff')).toBeNull();
    expect(field?.textContent).toContain('Status');
  });

  it('renders table_rows_diff as the row grid', () => {
    const { container } = renderFields([ROWS_FIELD]);
    const field = container.querySelector('.action-request-preview-field.table-preview-field');
    expect(field).toBeTruthy();
    expect(field?.querySelector('.action-request-preview-key')?.textContent).toBe('Rows: Insert 1 rows');
    expect(field?.querySelector('.table-rows-diff .sheet-diff-table.rows-variant')).toBeTruthy();
    expect(field?.querySelector('.table-columns-preview, .table-schema-diff')).toBeNull();
    expect(field?.querySelector('tr.sheet-diff-row-added')?.textContent).toContain('Lin');
  });

  it('stacks every table field under the one shared class', () => {
    const { container } = renderFields([COLUMNS_FIELD, SCHEMA_FIELD, ROWS_FIELD]);
    const fields = container.querySelectorAll('.action-request-preview-field');
    expect(fields).toHaveLength(3);
    fields.forEach((field) => {
      expect(field.className).toBe('action-request-preview-field table-preview-field');
    });
  });

  it('tableFieldLabel: the count for the column table, key: value otherwise', () => {
    const two = { ...COLUMNS_FIELD, columns: [...COLUMNS_FIELD.columns!, ...COLUMNS_FIELD.columns!] };
    expect(tableFieldLabel(two)).toBe('Columns (2)');
    expect(tableFieldLabel(SCHEMA_FIELD)).toBe('Changes: 1 operation: Renamed column State to Status');
    expect(tableFieldLabel(ROWS_FIELD)).toBe('Rows: Insert 1 rows');
  });

  it('falls back to the plain value when the payload is missing', () => {
    const fields: PreviewField[] = [
      { key: 'Columns', value: '1 column: Status (enum)', type: 'table_columns' },
      { key: 'Changes', value: '1 operation', type: 'table_schema_diff' },
      { key: 'Rows', value: 'Insert 1 rows', type: 'table_rows_diff' },
    ];
    const { container } = renderFields(fields);
    const values = Array.from(container.querySelectorAll('.action-request-preview-value'))
      .map((el) => el.textContent);
    expect(values).toEqual(['1 column: Status (enum)', '1 operation', 'Insert 1 rows']);
    expect(container.querySelector('.table-columns-table')).toBeNull();
    expect(container.querySelector('.table-schema-diff')).toBeNull();
    expect(container.querySelector('.table-rows-diff')).toBeNull();
  });
});
