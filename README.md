# Tsumego Workbooks

A template for creating tsumego workbooks.

Largely based on this other project: [@psygo/tecnicas_de_go](https://github.com/psygo/tecnicas_de_go).

## Development Setup

File paths are very annoying in TeX. And, for this project, we need to set up a global file path because each folder uses its own level as its reference. It's a bit repetitive, the other way around would be to define an environment variable on the terminal. For now, this is what I've been doing at the top of each file:

```tex
\def\repoPath{pathToTheRepo}
\usepackage{\repoPath/config}
```

## Generating SVG Images from the PDF

You can open the PDF through a graphical editor, such as [Inkscape](https://inkscape.org/), and then export a selection as an SVG.

But you can also use the `standalone` document type to export the diagram from a PDF to an SVG:

```sh
pdftocairo -svg filename.pdf filename.svg
```

## References

- [@psygo/tecnicas_de_go](https://github.com/psygo/tecnicas_de_go)
- [@psygo/latex_shogi](https://github.com/psygo/latex_shogi)
- [@101books/101books.github.io](https://github.com/101books/101books.github.io)
  - [101books.github.io](https://101books.github.io/)
