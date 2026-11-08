# Schemas

Scrimmage ships JSON Schema files inside the `scrimmage-core` package.
JSON Schema is the standard format for describing the shape of a JSON document.
Editors, CI checks and API tools read it.

This page is built from those files each time the site is built, so it matches the code of that build.
Each section below lists the keys of every table in one schema, then gives the whole file.
You can also download each file from its section.

`scrimmage schema` prints the schema of `scrimmage.toml` that matches your installed version.
An editor can then check the file as you type; see [Checking the file in your editor](configuration.md#checking-the-file-in-your-editor).

A schema checks shape only: keys, types and the form of a text.
Scrimmage itself checks more when it reads a file, so a value can fit a schema and still be refused.

<!-- shipped schemas -->
