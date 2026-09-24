// Curated seed list: English Wikipedia article titles and a curatorial
// grouping. Only the *titles* are chosen here; every bio, image, date and
// relationship is fetched from the article itself. Titles that do not resolve
// (or resolve to a disambiguation page) are reported and skipped.
//
// One-hop expansion (ingest/run.js) then adds family members named in these
// articles' infoboxes, typed from Wikipedia's own short description.

const group = (type, titles) => titles.map((title) => ({ title, type }));

export const SEEDS = [
  ...group('primordial', [
    'Chaos (cosmogony)', 'Gaia', 'Uranus (mythology)', 'Tartarus', 'Eros', 'Erebus (mythology)',
    'Nyx', 'Aether (mythology)', 'Hemera', 'Pontus (mythology)', 'Ourea', 'Chronos', 'Ananke (mythology)',
  ]),
  ...group('titan', [
    'Cronus', 'Rhea (mythology)', 'Oceanus', 'Tethys (mythology)', 'Hyperion (Titan)', 'Theia',
    'Coeus', 'Phoebe (Titan)', 'Crius', 'Mnemosyne', 'Themis', 'Iapetus', 'Atlas (mythology)',
    'Prometheus', 'Epimetheus', 'Menoetius', 'Metis (mythology)', 'Leto', 'Helios', 'Selene', 'Eos',
    'Asteria (Titaness)', 'Dione (mythology)',
  ]),
  ...group('olympian', [
    'Zeus', 'Hera', 'Poseidon', 'Demeter', 'Athena', 'Apollo', 'Artemis', 'Ares', 'Aphrodite',
    'Hephaestus', 'Hermes', 'Dionysus', 'Hestia',
  ]),
  ...group('deity', [
    'Hades', 'Persephone', 'Hecate', 'Pan (god)', 'Nemesis', 'Iris (mythology)', 'Hebe (mythology)',
    'Eileithyia', 'Asclepius', 'Muses', 'Charites', 'Moirai', 'Erinyes', 'Nike (mythology)', 'Tyche',
    'Hypnos', 'Thanatos', 'Morpheus', 'Triton (mythology)', 'Amphitrite', 'Nereus', 'Thetis',
    'Proteus', 'Harmonia (mythology)', 'Hesperides', 'Pleiades (Greek mythology)', 'Aeolus',
    'Boreas', 'Zephyrus', 'Eris (mythology)', 'Styx', 'Priapus', 'Hygieia', 'Calliope', 'Clio',
    'Calypso (mythology)', 'Circe', 'Nereids', 'Oceanids', 'Horae', 'Castor and Pollux',
  ]),
  ...group('hero', [
    'Heracles', 'Perseus', 'Theseus', 'Jason', 'Achilles', 'Odysseus', 'Bellerophon', 'Orpheus',
    'Atalanta', 'Cadmus', 'Oedipus', 'Agamemnon', 'Menelaus', 'Hector', 'Aeneas', 'Ajax the Great',
    'Ajax the Lesser', 'Diomedes', 'Patroclus', 'Nestor (mythology)', 'Meleager', 'Peleus',
    'Philoctetes', 'Neoptolemus', 'Telamon', 'Teucer', 'Hippolyta', 'Penthesilea',
    'Memnon (mythology)', 'Orestes', 'Telemachus', 'Palamedes', 'Idomeneus of Crete',
  ]),
  ...group('mortal', [
    'Paris (mythology)', 'Priam', 'Hecuba', 'Andromache', 'Cassandra', 'Helen of Troy', 'Penelope',
    'Clytemnestra', 'Electra', 'Iphigenia', 'Medea', 'Ariadne', 'Phaedra',
    'Hippolytus (son of Theseus)', 'Minos', 'Pasiphaë', 'Daedalus', 'Icarus', 'Andromeda', 'Danaë',
    'Alcmene', 'Leda (mythology)', 'Europa (consort of Zeus)', 'Io (mythology)', 'Semele', 'Antigone',
    'Jocasta', 'Laius', 'Pelops', 'Tantalus', 'Atreus', 'Thyestes', 'Sisyphus',
    'Narcissus (mythology)', 'Echo (mythology)', 'Pandora', 'Deucalion', 'Pyrrha',
    'Ganymede (mythology)', 'Adonis', 'Psyche (mythology)', 'Arachne', 'Niobe', 'Actaeon', 'Midas',
    'Tiresias', 'Aegeus', 'Eurydice', 'Laocoön', 'Troilus', 'Briseis', 'Aegisthus', 'Hermione (mythology)',
    'Ixion', 'Endymion (mythology)', 'Hyacinth (mythology)', 'Orion (mythology)', 'Callisto (mythology)',
    'Tyndareus', 'Acrisius', 'Polydectes', 'Pentheus', 'Creon (king of Thebes)', 'Eurystheus', 'Deianira',
    'Aeson', 'Pelias', 'Aeëtes', 'Nausicaa', 'Alcinous', 'Laertes', 'Anchises',
  ]),
  ...group('creature', [
    'Medusa', 'Gorgon', 'Minotaur', 'Cerberus', 'Lernaean Hydra', 'Chimera (mythology)', 'Pegasus',
    'Cyclopes', 'Polyphemus', 'Sphinx', 'Scylla', 'Charybdis', 'Siren (mythology)', 'Harpy', 'Typhon',
    'Echidna (mythology)', 'Nemean lion', 'Ladon (mythology)', 'Python (mythology)', 'Centaur',
    'Chiron', 'Hecatoncheires', 'Giants (Greek mythology)', 'Stymphalian birds', 'Erymanthian boar',
    'Cretan Bull', 'Ceryneian Hind', 'Geryon', 'Orthrus', 'Talos', 'Graeae', 'Argus Panoptes',
    'Colchian dragon', 'Calydonian boar', 'Chrysaor', 'Mares of Diomedes', 'Satyr',
  ]),
  ...group('event', [
    'Titanomachy', 'Gigantomachy', 'Trojan War', 'Judgement of Paris', 'Labours of Hercules',
    'Argonauts', 'Golden Fleece', 'Seven against Thebes', 'Epigoni', 'Trojan Horse',
    "Pandora's box", 'Abduction of Persephone', 'Centauromachy', 'Amazonomachy', 'Rape of Europa',
    'Wedding of Peleus and Thetis', 'Golden apple', 'Sack of Troy', 'Returns from Troy',
    'Choice of Heracles',
  ]),
];
// Events are happenings inside the myths. Literary works (Iliad, Theogony, …)
// are deliberately not nodes: their composition dates would mix two timelines.
