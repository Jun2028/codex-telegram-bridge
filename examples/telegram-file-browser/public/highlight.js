'use strict';
// A small syntax colourer. It only classifies text into spans; the page sets
// their text with textContent, so file contents can never run as markup.
// No external library: the page's content policy allows only its own scripts.
(() => {
  const words = list => '\\b(?:' + list.trim().split(/\s+/).join('|') + ')\\b';
  const NUMBER = '\\b(?:0[xX][\\da-fA-F_]+|\\d[\\d_]*(?:\\.[\\d_]+)?(?:[eE][+-]?\\d+)?)\\b';
  const DOUBLE = '"(?:[^"\\\\\\n]|\\\\.)*"?';
  const SINGLE = "'(?:[^'\\\\\\n]|\\\\.)*'?";

  // Each language is an ordered list of [token class, pattern]. Earlier entries win.
  const LANGUAGES = {
    javascript: [
      ['comment', '//[^\\n]*|/\\*[\\s\\S]*?(?:\\*/|(?![\\s\\S]))'],
      ['string', '`(?:[^`\\\\]|\\\\[\\s\\S])*`?|' + DOUBLE + '|' + SINGLE],
      ['keyword', words(`async await break case catch class const continue debugger default delete do else
        export extends finally for from function if import in instanceof let new of return static super
        switch this throw try typeof var void while with yield interface type enum implements
        public private protected readonly as`)],
      ['literal', words('true false null undefined NaN Infinity')],
      ['number', NUMBER],
      ['name', '\\b[A-Za-z_$][\\w$]*(?=\\s*\\()'],
    ],
    python: [
      ['comment', '#[^\\n]*'],
      ['string', '(?:[rRbBfFuU]{0,2})(?:"""[\\s\\S]*?(?:"""|(?![\\s\\S]))|\'\'\'[\\s\\S]*?(?:\'\'\'|(?![\\s\\S]))|' + DOUBLE + '|' + SINGLE + ')'],
      ['keyword', words(`and as assert async await break class continue def del elif else except finally for
        from global if import in is lambda nonlocal not or pass raise return try while with yield match case`)],
      ['literal', words('True False None self cls')],
      ['number', NUMBER],
      ['name', '@[\\w.]+|\\b[A-Za-z_]\\w*(?=\\s*\\()'],
    ],
    json: [
      ['attr', '"(?:[^"\\\\\\n]|\\\\.)*"(?=\\s*:)'],
      ['string', DOUBLE],
      ['literal', words('true false null')],
      ['number', '-?' + NUMBER],
    ],
    yaml: [
      ['comment', '#[^\\n]*'],
      ['string', DOUBLE + '|' + SINGLE],
      ['attr', '^[ \\t-]*[\\w.\\-/ ]+?(?=\\s*:(?:\\s|$))|^\\s*\\[[^\\]\\n]+\\]'],
      ['literal', words('true false null yes no on off')],
      ['number', NUMBER],
    ],
    shell: [
      ['comment', '#[^\\n]*'],
      ['string', DOUBLE + '|' + SINGLE],
      ['keyword', words(`if then else elif fi for while until do done case esac function in return exit
        export local readonly set unset source echo cd`)],
      ['name', '\\$\\{[^}\\n]*\\}|\\$[\\w@#?*!]+'],
      ['number', NUMBER],
    ],
    css: [
      ['comment', '/\\*[\\s\\S]*?(?:\\*/|(?![\\s\\S]))'],
      ['string', DOUBLE + '|' + SINGLE],
      ['keyword', '@[\\w-]+'],
      ['number', '#[\\da-fA-F]{3,8}\\b|-?\\b\\d[\\d.]*(?:px|em|rem|vh|vw|dvh|%|s|ms|deg|fr)?'],
      ['attr', '[\\w-]+(?=\\s*:)'],
      ['name', '--[\\w-]+'],
    ],
    markup: [
      ['comment', '<!--[\\s\\S]*?(?:-->|(?![\\s\\S]))'],
      ['string', DOUBLE + '|' + SINGLE],
      ['tag', '</?[A-Za-z][\\w:-]*|/?>'],
      ['attr', '\\b[\\w:-]+(?==)'],
    ],
    markdown: [
      ['string', '```[\\s\\S]*?(?:```|(?![\\s\\S]))|`[^`\\n]+`'],
      ['heading', '^#{1,6} [^\\n]*'],
      ['keyword', '^\\s*(?:[-*+]|\\d+\\.) (?=\\S)|^>+ ?'],
      ['name', '\\[[^\\]\\n]+\\]\\([^)\\n]+\\)|https?://[^\\s)>]+'],
      ['strong', '\\*\\*[^*\\n]+\\*\\*|__[^_\\n]+__'],
    ],
    // Delimited data: colour by column so wide tables stay readable.
    csv: [['string', DOUBLE], ['number', '-?' + NUMBER], ['comment', '[,\\t;]']],
    log: [
      ['comment', '\\b\\d{4}-\\d\\d-\\d\\d[T ][\\d:.,]+(?:Z|[+-]\\d\\d:?\\d\\d)?'],
      ['tag', words('ERROR FATAL CRITICAL FAILED Traceback Exception')],
      ['keyword', words('WARN WARNING')],
      ['literal', words('INFO DEBUG OK')],
      ['string', DOUBLE],
      ['number', NUMBER],
    ],
  };
  const BY_EXTENSION = {
    js: 'javascript', cjs: 'javascript', mjs: 'javascript', ts: 'javascript', tsx: 'javascript', jsx: 'javascript',
    py: 'python', json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'yaml', env: 'shell', sh: 'shell', bash: 'shell',
    css: 'css', html: 'markup', htm: 'markup', svg: 'markup', xml: 'markup',
    md: 'markdown', csv: 'csv', tsv: 'csv', log: 'log',
  };
  const PROSE = new Set(['markdown', 'log', undefined]);
  const compiled = {};
  function grammar(language) {
    if (!compiled[language]) {
      const rules = LANGUAGES[language];
      compiled[language] = {
        classes: rules.map(rule => rule[0]),
        pattern: new RegExp(rules.map(rule => '(' + rule[1] + ')').join('|'), 'gm'),
      };
    }
    return compiled[language];
  }

  const HIGHLIGHT_LIMIT = 300000;  // Larger files are shown plain: still readable, never slow.

  // Fill `target` with coloured spans for `text`. Returns the language used, if any.
  function render(target, text, fileName) {
    const extension = fileName.includes('.') ? fileName.split('.').pop().toLowerCase() : '';
    const language = BY_EXTENSION[extension];
    target.replaceChildren();
    if (!language || text.length > HIGHLIGHT_LIMIT) { target.textContent = text; return undefined; }
    const { classes, pattern } = grammar(language);
    const fragment = document.createDocumentFragment();
    let last = 0;
    pattern.lastIndex = 0;
    for (let match; (match = pattern.exec(text));) {
      if (!match[0]) { pattern.lastIndex++; continue; }
      if (match.index > last) fragment.append(text.slice(last, match.index));
      const span = document.createElement('span');
      span.className = 'tok-' + classes[match.slice(1).findIndex(group => group !== undefined)];
      span.textContent = match[0];
      fragment.append(span);
      last = match.index + match[0].length;
    }
    if (last < text.length) fragment.append(text.slice(last));
    target.append(fragment);
    return language;
  }

  window.Highlight = { render, isProse: fileName => {
    const extension = fileName.includes('.') ? fileName.split('.').pop().toLowerCase() : '';
    return PROSE.has(BY_EXTENSION[extension]);
  } };
})();
