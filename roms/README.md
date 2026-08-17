# ROMs (bring your own)

PokéBench never ships a ROM. Place your **legally-obtained** Pokémon Red cartridge dump
here as:

```
roms/pokemon_red.gb
```

(or point the `POKEBENCH_ROM` env var / `--rom` flag anywhere else).

The harness verifies the file against the sha1 declared by the pret disassemblies and
logs a warning on mismatch (Blue works too; **Yellow does not** — different RAM layout):

- Red (UE): `ea9bcae617fdf159b045185467ae58b2e4a48b9a`
- Blue (UE): `d7037c83e1ae5b39bde3c30787637ba1d4c48ce2`

Everything in this directory except this README is git-ignored.
