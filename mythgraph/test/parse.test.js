import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  parseInfobox, familyFromInfobox, extractLinks, leadSection, stripRefs, imageCandidates,
  isPublicDomain, imageRecord, pickFunFact, firstSentences, typeFromDescription, normalizeTitle, slugify,
} from '../ingest/parse.js';

// Placeholder names only: these fixtures test wikitext syntax, not myth content.
const WT = `{{Short description|Test}}
{{Infobox deity
| name = Alpha
| image = Alpha vase.jpg
| parents = [[Beta (test)|Beta]] and [[Gamma]]<ref>{{cite book|title=[[Ignored ref link]]}}</ref>
| siblings = {{hlist|[[Delta]]|[[Epsilon#Section|Eps]]}}
| consort = [[Zeta]]
| children = [[Eta]], [[Theta]]
| symbol = [[File:Icon.svg|20px]] lightning
}}
'''Alpha''' is linked to [[Iota]] and [[Kappa (test)|Kappa]] and [[Category:Nope]].
[[File:Second image.JPG|thumb|caption with [[Lambda]]]]

== Myths ==
Body text with [[Mu]].
<gallery>
File:Gallery one.png|cap
</gallery>`;

test('parseInfobox reads top-level params and ignores refs', () => {
  const box = parseInfobox(WT);
  assert.equal(box.name, 'Infobox deity');
  assert.match(box.params.parents, /Beta/);
  assert.doesNotMatch(box.params.parents, /Ignored/);
  assert.match(box.params.siblings, /hlist/);
});

test('familyFromInfobox maps params to directed relations', () => {
  const fam = familyFromInfobox(parseInfobox(WT));
  const pick = (t) => fam.filter((f) => f.target === t).map((f) => `${f.rel}:${f.dir}`);
  assert.deepEqual(pick('Beta (test)'), ['parent:in']);
  assert.deepEqual(pick('Gamma'), ['parent:in']);
  assert.deepEqual(pick('Epsilon'), ['sibling:out']);
  assert.deepEqual(pick('Zeta'), ['consort:out']);
  assert.deepEqual(pick('Eta'), ['parent:out']);
  assert.equal(fam.some((f) => f.target === 'Ignored ref link'), false);
});

test('lead links exclude namespaces and later sections', () => {
  const links = extractLinks(leadSection(stripRefs(WT)));
  assert.ok(links.includes('Iota'));
  assert.ok(links.includes('Kappa (test)'));
  assert.ok(!links.includes('Mu'));
  assert.ok(!links.some((l) => /^(Category|File):/.test(l)));
});

test('image candidates keep order, drop svg', () => {
  assert.deepEqual(imageCandidates(WT), ['File:Alpha vase.jpg', 'File:Second image.JPG', 'File:Gallery one.png']);
});

test('only public domain / CC0 images pass', () => {
  assert.equal(isPublicDomain({ License: { value: 'pd' } }), true);
  assert.equal(isPublicDomain({ LicenseShortName: { value: 'Public domain' } }), true);
  assert.equal(isPublicDomain({ License: { value: 'cc0' } }), true);
  assert.equal(isPublicDomain({ LicenseShortName: { value: 'PD-Art (PD-old-100)' } }), true);
  assert.equal(isPublicDomain({ License: { value: 'cc-by-sa-4.0' }, LicenseShortName: { value: 'CC BY-SA 4.0' } }), false);
  assert.equal(isPublicDomain({}), false);
  assert.equal(isPublicDomain(undefined), false);
  const rec = imageRecord({
    title: 'File:X.jpg',
    imageinfo: [{ url: 'u', thumburl: 't', width: 900, height: 600, descriptionurl: 'd',
      extmetadata: { License: { value: 'pd' }, Artist: { value: '<a href="x">Some Painter</a>' }, DateTimeOriginal: { value: '1600' } } }],
  });
  assert.equal(rec.artist, 'Some Painter');
  assert.equal(rec.thumb_url, 't');
  assert.equal(imageRecord({ title: 'File:Y.jpg', imageinfo: [{ extmetadata: { License: { value: 'cc-by-4.0' } } }] }), null);
});

test('fun fact is a verbatim opening of a named section', () => {
  const text = 'Lead paragraph here that is long enough to count as text.\n\n== Etymology ==\nThe name derives from an old word meaning something. A second sentence follows here. A third one.\n\n== Other ==\nx';
  const f = pickFunFact(text);
  assert.equal(f.section, 'Etymology');
  assert.ok(text.includes(f.text));
  assert.equal(pickFunFact('Only a lead, no sections at all.'), null);
  assert.equal(firstSentences('x'.repeat(400), 300), null, 'never cuts a sentence');
});

test('expansion typing requires a Greek-myth description', () => {
  assert.equal(typeFromDescription('Greek goddess of something'), 'deity');
  assert.equal(typeFromDescription('Titan in Greek mythology'), 'titan');
  assert.equal(typeFromDescription('Monster in Greek mythology'), 'creature');
  assert.equal(typeFromDescription('King in Greek mythology'), 'mortal');
  assert.equal(typeFromDescription('American actor'), null);
  assert.equal(typeFromDescription(null), null);
});

test('titles normalise and slug', () => {
  assert.equal(normalizeTitle('foo_bar#baz'), 'Foo bar');
  assert.equal(normalizeTitle('#anchor'), null);
  assert.equal(slugify('Pasiphaë'), 'pasiphae');
  assert.equal(slugify('Chaos (cosmogony)'), 'chaos-cosmogony');
});
