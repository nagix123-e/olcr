"""Deterministic table transformations, shared by the API and tests."""
import json
import math

def transform_rows(rows: list[dict], operation: str, column: str = '', target: str = '', value: str = '') -> list[dict]:
    columns = list(dict.fromkeys(k for row in rows for k in row))
    if (operation in {'rename', 'drop', 'number', 'merge'} or operation == 'deduplicate' and column) and column not in columns:
        raise ValueError('COLUMN_NOT_FOUND')
    if operation in {'rename', 'merge'} and (not target.strip() or len(target) > 120):
        raise ValueError('INVALID_TARGET_COLUMN')
    if operation == 'rename' and target != column and target in columns:
        raise ValueError('COLUMN_ALREADY_EXISTS')
    if operation == 'merge' and (target not in columns or target == column):
        raise ValueError('CHOOSE_TWO_EXISTING_COLUMNS')
    if operation == 'reorder':
        try: order = json.loads(value)
        except (ValueError, TypeError): raise ValueError('INVALID_COLUMN_ORDER') from None
        if not isinstance(order, list) or any(not isinstance(k, str) for k in order) or len(order) != len(columns) or set(order) != set(columns):
            raise ValueError('INVALID_COLUMN_ORDER')
        return [{key: row.get(key, '') for key in order} for row in rows]
    result = []
    seen = set()
    for original in rows:
        row = dict(original)
        if operation == 'rename': row = {target if k == column else k: v for k, v in row.items()}
        elif operation == 'drop': row.pop(column, None)
        elif operation == 'number':
            cell = row.get(column)
            if cell not in (None, ''):
                try:
                    number = float(str(cell).replace(',', ''))
                    if not math.isfinite(number): raise ValueError()
                    row[column] = int(number) if number.is_integer() else number
                except (ValueError, TypeError): raise ValueError('COLUMN_CONTAINS_NON_NUMERIC_VALUE') from None
        elif operation == 'merge': row[target] = value.join(('' if row.get(k) is None else str(row.get(k))) for k in (column, target))
        elif operation == 'drop_empty':
            if all(v in (None, '', [], {}) for v in row.values()): continue
        elif operation == 'deduplicate':
            key = json.dumps(row.get(column) if column else row, sort_keys=True, ensure_ascii=False)
            if key in seen: continue
            seen.add(key)
        else: raise ValueError('UNKNOWN_TABLE_OPERATION')
        result.append(row)
    return result

def xlsx_bytes(rows, columns: list[str]) -> bytes:
    """A portable inline-string workbook; scraped text never becomes a formula."""
    from io import BytesIO
    from xml.sax.saxutils import escape
    from zipfile import ZipFile, ZIP_DEFLATED
    def letter(n):
        result = ''
        while n:
            n, r = divmod(n - 1, 26)
            result = chr(65 + r) + result
        return result
    def cell(v, coordinate):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            return f'<c r="{coordinate}"><v>{v}</v></c>'
        text = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else ('' if v is None else str(v))
        text = ''.join(c for c in text[:32767] if ord(c) >= 32 or c in '\t\n\r')
        return f'<c r="{coordinate}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'
    sheet = ['<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    for i, values in enumerate([columns], 1):
        sheet.append(f'<row r="{i}">'+''.join(cell(v, letter(j)+str(i)) for j,v in enumerate(values,1))+'</row>')
    size = sum(len(fragment) for fragment in sheet)
    for i, row in enumerate(rows, 2):
        fragment = f'<row r="{i}">'+''.join(cell(row.get(k), letter(j)+str(i)) for j,k in enumerate(columns,1))+'</row>'
        size += len(fragment)
        if size > 32_000_000: raise ValueError('EXCEL_EXPORT_SIZE_LIMIT_USE_CSV')
        sheet.append(fragment)
    sheet.append('</sheetData></worksheet>')
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Results" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr('xl/worksheets/sheet1.xml', ''.join(sheet))
    return output.getvalue()
