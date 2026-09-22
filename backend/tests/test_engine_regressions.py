"""Regression coverage for positioned glyphs, multiline edits and page margins."""
import os
import json
from pathlib import Path
import fitz
import pytest
from engine.document import process
from engine.search import Finder


def make_pdf(lines, width=595):
    doc=fitz.open();page=doc.new_page(width=width,height=842)
    for x,y,text,positioned in lines:
        if positioned:
            for char in text:
                page.insert_text((x,y),char,fontsize=11)
                x+=fitz.get_text_length(char,fontsize=11)
        else:page.insert_text((x,y),text,fontsize=11)
    result=doc.tobytes();doc.close();return result


def text_of(raw):
    with fitz.open(stream=raw,filetype='pdf') as doc:
        return '\n'.join(page.get_text() for page in doc)


def test_positioned_glyphs_join_but_not_columns_or_lines():
    raw=make_pdf([(72,72,'Old Client',True),(300,72,'Other column',True),(72,100,'Keep this',True)])
    result=process(raw,[Finder(id='name',query='Old Client',replace='New Company')])
    assert result.applied==1 and result.refused==0
    assert result.verification['acceptable']
    text=text_of(result.pdf_bytes)
    assert 'New Company' in text and 'Old Client' not in text
    assert 'Other column' in text and 'Keep this' in text
    result=process(raw,[Finder(id='no',query='Client Other',replace='Wrong')])
    assert result.applied==0


@pytest.mark.parametrize('target',['New Street\nNew City','New Street\nNew City\nNew Country'])
def test_multiline_uses_existing_baselines(target):
    raw=make_pdf([(72,72,'Old Street',False),(72,87,'Old City',False),(72,130,'Do not change 123.45',False)])
    result=process(raw,[Finder(id='addr',kind='regex',query=r'Old Street\s+Old City',replace=target)])
    assert result.applied==1 and result.refused==0
    assert result.verification['acceptable']
    text=text_of(result.pdf_bytes)
    assert 'New Street' in text and 'New City' in text
    assert ('New Country' in text)==('New Country' in target)
    assert 'Old Street' not in text and 'Old City' not in text
    assert 'Do not change 123.45' in text


def test_right_edge_keeps_full_size_and_stays_inside_page():
    raw=make_pdf([(558,72,'OLD',False)])
    result=process(raw,[Finder(id='right',query='OLD',replace='New Company')])
    assert result.applied==1 and result.refused==0
    assert result.verification['acceptable']
    with fitz.open(stream=result.pdf_bytes,filetype='pdf') as doc:
        spans=[s for b in doc[0].get_text('dict')['blocks'] if 'lines' in b for l in b['lines'] for s in l['spans']]
        span=next(s for s in spans if 'New Company' in s['text'])
        assert span['bbox'][2]<=doc[0].rect.width
        assert abs(span['size']-11)<0.01
        assert span['bbox'][2]-span['bbox'][0]>60


def test_private_invoice_regressions():
    directory=os.environ.get('PDF_REGRESSION_FIXTURES')
    if not directory:pytest.skip('Private fixtures are supplied outside the repository')
    root=Path(directory)
    finders=[Finder(id=str(i),**r) for i,r in enumerate(json.loads((root/'rules.json').read_text()))]
    counts={'Anthropic':2,'Cursor':2,'Fal':2,'MassedCompute':0,'Onoff':0,'OpenAI':2,'Seeweb':2,'Shadeform_Direct':1,'Shadeform_Google':1,'Termius':0,'Vercel':1,'Worldstream':2}
    for name,count in counts.items():
        source=next((root/name).glob('*.pdf')).read_bytes()
        result=process(source,finders)
        assert result.applied==count,(name,result.to_dict())
        assert result.refused==0,(name,result.to_dict())
        assert not result.notdef,name
        if count:
            assert result.verification['acceptable'],name
            assert 'Chalco LABS' in text_of(result.pdf_bytes),name
        else:
            assert text_of(source)==text_of(result.pdf_bytes),name
        with fitz.open(stream=result.pdf_bytes,filetype='pdf') as doc:
            for page in doc:
                for trace in page.get_texttrace():
                    for char in trace['chars']:
                        if char[0]>32:
                            assert char[3][2]<=page.rect.width+0.5,(name,char)


def test_explicit_push_overflow_is_rejected_by_verification():
    raw=make_pdf([(558,72,'OLD',False)])
    result=process(raw,[Finder(id='right',query='OLD',replace='A very long company name')],
                   fits={'p0-1':'push'},anchors={'p0-1':'left'})
    assert result.verification['acceptable'] is False
    assert result.verification['outside_page_glyphs'] > 0
