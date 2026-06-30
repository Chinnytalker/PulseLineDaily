(function () {
  'use strict';

  var TOOLBAR = [
    'heading', '|',
    'bold', 'italic', 'link', '|',
    'bulletedList', 'numberedList', '|',
    'outdent', 'indent', '|',
    'blockQuote', 'insertTable', 'mediaEmbed', '|',
    'undo', 'redo'
  ];

  var HEADING_OPTIONS = [
    { model: 'paragraph', title: 'Paragraph', class: 'ck-heading_paragraph' },
    { model: 'heading2', view: 'h2', title: 'Heading 2', class: 'ck-heading_heading2' },
    { model: 'heading3', view: 'h3', title: 'Heading 3', class: 'ck-heading_heading3' },
    { model: 'heading4', view: 'h4', title: 'Heading 4', class: 'ck-heading_heading4' }
  ];

  function attachEditor(id, toolbarItems) {
    var textarea = document.getElementById(id);
    if (!textarea) return;

    ClassicEditor
      .create(textarea, {
        toolbar: { items: toolbarItems || TOOLBAR },
        heading: { options: HEADING_OPTIONS },
        table: { contentToolbar: ['tableColumn', 'tableRow', 'mergeTableCells'] }
      })
      .then(function (editor) {
        // Keep textarea in sync so Django form submission works
        var form = textarea.closest('form');
        if (form) {
          form.addEventListener('submit', function () {
            textarea.value = editor.getData();
          });
        }
      })
      .catch(function (err) { console.error('[CKEditor]', err); });
  }

  document.addEventListener('DOMContentLoaded', function () {
    // Main article body — full toolbar
    attachEditor('id_body', TOOLBAR);
  });
})();
