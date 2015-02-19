from __future__ import absolute_import, unicode_literals
# this ensures that any render / __str__ methods returning HTML via calls to mark_safe / format_html
# return a SafeText, not SafeBytes; necessary so that it doesn't get re-encoded when the template engine
# calls force_text, which would cause it to lose its 'safe' flag

import collections
from importlib import import_module

from django.core.exceptions import ValidationError, ImproperlyConfigured
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.utils.text import capfirst
from django.utils.encoding import force_text, python_2_unicode_compatible
from django.utils.functional import cached_property
from django.template.loader import render_to_string
from django import forms
from django.forms.utils import ErrorList

import six

from wagtail.wagtailcore.utils import escape_script
from wagtail.wagtailcore.rich_text import expand_db_html

from .utils import indent, js_dict


# =========================================
# Top-level superclasses and helper objects
# =========================================


class BaseBlock(type):
    def __new__(mcs, name, bases, attrs):
        meta_class = attrs.pop('Meta', None)

        cls = super(BaseBlock, mcs).__new__(mcs, name, bases, attrs)

        base_meta_class = getattr(cls, '_meta_class', None)
        bases = tuple(cls for cls in [meta_class, base_meta_class] if cls) or ()
        cls._meta_class = type(str(name + 'Meta'), bases + (object, ), {})

        return cls


class Block(six.with_metaclass(BaseBlock, object)):
    name = ''
    creation_counter = 0

    class Meta:
        label = None
        icon = "placeholder"
        classname = None

    """
    Setting a 'dependencies' list serves as a shortcut for the common case where a complex block type
    (such as struct, list or stream) relies on one or more inner block objects, and needs to ensure that
    the responses from the 'media' and 'html_declarations' include the relevant declarations for those inner
    blocks, as well as its own. Specifying these inner block objects in a 'dependencies' list means that
    the base 'media' and 'html_declarations' methods will return those declarations; the outer block type can
    then add its own declarations to the list by overriding those methods and using super().
    """
    dependencies = []

    def __new__(cls, *args, **kwargs):
        # adapted from django.utils.deconstruct.deconstructible; capture the arguments
        # so that we can return them in the 'deconstruct' method
        obj = super(Block, cls).__new__(cls, *args, **kwargs)
        obj._constructor_args = (args, kwargs)
        return obj

    def all_blocks(self):
        """
        Return a list consisting of self and all block objects that are direct or indirect dependencies
        of this block
        """
        result = [self]
        for dep in self.dependencies:
            result.extend(dep.all_blocks())
        return result

    def all_media(self):
        media = forms.Media()
        for block in self.all_blocks():
            media += block.media
        return media

    def all_html_declarations(self):
        declarations = filter(bool, [block.html_declarations() for block in self.all_blocks()])
        return mark_safe('\n'.join(declarations))

    def __init__(self, **kwargs):
        self.meta = self._meta_class()

        for attr, value in kwargs.items():
            setattr(self.meta, attr, value)

        # Increase the creation counter, and save our local copy.
        self.creation_counter = Block.creation_counter
        Block.creation_counter += 1
        self.definition_prefix = 'blockdef-%d' % self.creation_counter

        self.label = self.meta.label or ''

    def set_name(self, name):
        self.name = name
        if not self.meta.label:
            self.label = capfirst(force_text(name).replace('_', ' '))

    @property
    def media(self):
        return forms.Media()

    def html_declarations(self):
        """
        Return an HTML fragment to be rendered on the form page once per block definition -
        as opposed to once per occurrence of the block. For example, the block definition
            ListBlock(label="Shopping list", CharBlock(label="Product"))
        needs to output a <script type="text/template"></script> block containing the HTML for
        a 'product' text input, to that these can be dynamically added to the list. This
        template block must only occur once in the page, even if there are multiple 'shopping list'
        blocks on the page.

        Any element IDs used in this HTML fragment must begin with definition_prefix.
        (More precisely, they must either be definition_prefix itself, or begin with definition_prefix
        followed by a '-' character)
        """
        return ''

    def js_initializer(self):
        """
        Returns a Javascript expression string, or None if this block does not require any
        Javascript behaviour. This expression evaluates to an initializer function, a function that
        takes the ID prefix and applies JS behaviour to the block instance with that value and prefix.

        The parent block of this block (or the top-level page code) must ensure that this
        expression is not evaluated more than once. (The resulting initializer function can and will be
        called as many times as there are instances of this block, though.)
        """
        return None

    def render_form(self, value, prefix='', errors=None):
        """
        Render the HTML for this block with 'value' as its content.
        """
        raise NotImplementedError('%s.render_form' % self.__class__)

    def value_from_datadict(self, data, files, prefix):
        raise NotImplementedError('%s.value_from_datadict' % self.__class__)

    def bind(self, value, prefix=None, errors=None):
        """
        Return a BoundBlock which represents the association of this block definition with a value
        and a prefix (and optionally, a ValidationError to be rendered).
        BoundBlock primarily exists as a convenience to allow rendering within templates:
        bound_block.render() rather than blockdef.render(value, prefix) which can't be called from
        within a template.
        """
        return BoundBlock(self, value, prefix=prefix, errors=errors)

    def prototype_block(self):
        """
        Return a BoundBlock that can be used as a basis for new empty block instances to be added on the fly
        (new list items, for example). This will have a prefix of '__PREFIX__' (to be dynamically replaced with
        a real prefix when it's inserted into the page) and a value equal to the block's default value.
        """
        return self.bind(self.meta.default, '__PREFIX__')

    def clean(self, value):
        """
        Validate value and return a cleaned version of it, or throw a ValidationError if validation fails.
        The thrown ValidationError instance will subsequently be passed to render() to display the
        error message; nested blocks therefore need to wrap child validations like this:
        https://docs.djangoproject.com/en/dev/ref/forms/validation/#raising-multiple-errors

        NB The ValidationError must have an error_list property (which can be achieved by passing a
        list or an individual error message to its constructor), NOT an error_dict -
        Django has problems nesting ValidationErrors with error_dicts.
        """
        return value

    def to_python(self, value):
        """
        Convert 'value' from a simple (JSON-serialisable) value to a (possibly complex) Python value to be
        used in the rest of the block API and within front-end templates . In simple cases this might be
        the value itself; alternatively, it might be a 'smart' version of the value which behaves mostly
        like the original value but provides a native HTML rendering when inserted into a template; or it
        might be something totally different (e.g. an image chooser will use the image ID as the clean
        value, and turn this back into an actual image object here).
        """
        return value

    def get_prep_value(self, value):
        """
        The reverse of to_python; convert the python value into JSON-serialisable form.
        """
        return value

    def render(self, value):
        """
        Return a text rendering of 'value', suitable for display on templates. By default, this will
        use a template if a 'template' property is specified on the block, and fall back on render_basic
        otherwise.
        """
        template = getattr(self.meta, 'template', None)
        if template:
            return render_to_string(template, {'self': value})
        else:
            return self.render_basic(value)

    def render_basic(self, value):
        """
        Return a text rendering of 'value', suitable for display on templates. render() will fall back on
        this if the block does not define a 'template' property.
        """
        return force_text(value)

    def get_searchable_content(self, value):
        """
        Returns a list of strings containing text content within this block to be used in a search engine.
        """
        return []

    def deconstruct(self):
        # adapted from django.utils.deconstruct.deconstructible
        module_name = self.__module__
        name = self.__class__.__name__

        # Make sure it's actually there and not an inner class
        module = import_module(module_name)
        if not hasattr(module, name):
            raise ValueError(
                "Could not find object %s in %s.\n"
                "Please note that you cannot serialize things like inner "
                "classes. Please move the object into the main module "
                "body to use migrations.\n"
                % (name, module_name))

        return (
            '%s.%s' % (module_name, name),
            self._constructor_args[0],
            self._constructor_args[1],
        )

    def __eq__(self, other):
        """
        The deep_deconstruct method in django.db.migrations.autodetector.MigrationAutodetector does not
        recurse into arbitrary lists and dicts. As a result, when it is passed a field such as:
            StreamField([
                ('heading', CharBlock()),
            ])
        the CharBlock object will be left in its constructed form. This causes problems when
        MigrationAutodetector compares two separate instances of the StreamField from different project
        states: since the CharBlocks are different objects, it will report a change where there isn't one.

        To prevent this, we implement the equality operator on Block instances such that the two CharBlocks
        are reported as equal. Since block objects are intended to be immutable with the exception of
        set_name(), it is sufficient to compare the 'name' property and the constructor args/kwargs of the
        two block objects. The 'deconstruct' method provides a convenient way to access the latter.
        """

        if not isinstance(other, Block):
            # if the other object isn't a block at all, it clearly isn't equal.
            return False

            # Note that we do not require the two blocks to be of the exact same class. This is because
            # we may wish the following blocks to be considered equal:
            #
            # class FooBlock(StructBlock):
            #     first_name = CharBlock()
            #     surname = CharBlock()
            #
            # class BarBlock(StructBlock):
            #     first_name = CharBlock()
            #     surname = CharBlock()
            #
            # FooBlock() == BarBlock() == StructBlock([('first_name', CharBlock()), ('surname': CharBlock())])
            #
            # For this to work, StructBlock will need to ensure that 'deconstruct' returns the same signature
            # in all of these cases, including reporting StructBlock as the path:
            #
            # FooBlock().deconstruct() == (
            #     'wagtail.wagtailcore.blocks.StructBlock',
            #     [('first_name', CharBlock()), ('surname': CharBlock())],
            #     {}
            # )
            #
            # This has the bonus side effect that the StructBlock field definition gets frozen into
            # the migration, rather than leaving the migration vulnerable to future changes to FooBlock / BarBlock
            # in models.py.

        return (self.name == other.name) and (self.deconstruct() == other.deconstruct())

    def __ne__(self, other):
        return not self.__eq__(other)

    # Making block instances hashable in a way that's consistent with __eq__ is non-trivial, because
    # self.deconstruct() is liable to contain unhashable data (e.g. lists and dicts). So let's set
    # Block to be explicitly unhashable - Python 3 will do this automatically when defining __eq__,
    # but Python 2 won't, and we'd like the behaviour to be consistent on both.
    __hash__ = None


class BoundBlock(object):
    def __init__(self, block, value, prefix=None, errors=None):
        self.block = block
        self.value = value
        self.prefix = prefix
        self.errors = errors

    def render_form(self):
        return self.block.render_form(self.value, self.prefix, errors=self.errors)

    def render(self):
        return self.block.render(self.value)


# ===========
# Field block
# ===========

class FieldBlock(Block):
    class Meta:
        default = None

    def render_form(self, value, prefix='', errors=None):
        widget = self.field.widget

        if self.label:
            label_html = format_html(
                """<label for={label_id}>{label}</label> """,
                label_id=widget.id_for_label(prefix), label=self.label
            )
        else:
            label_html = ''

        widget_attrs = {'id': prefix, 'placeholder': self.label}

        if hasattr(widget, 'render_with_errors'):
            widget_html = widget.render_with_errors(prefix, value, attrs=widget_attrs, errors=errors)
            widget_has_rendered_errors = True
        else:
            widget_html = widget.render(prefix, value, attrs=widget_attrs)
            widget_has_rendered_errors = False

        return render_to_string('wagtailadmin/block_forms/field.html', {
            'name': self.name,
            'label': self.label,
            'classes': self.meta.classname,
            'widget': widget_html,
            'label_tag': label_html,
            'field': self.field,
            'errors': errors if (not widget_has_rendered_errors) else None
        })

    def value_from_datadict(self, data, files, prefix):
        return self.to_python(self.field.widget.value_from_datadict(data, files, prefix))

    def clean(self, value):
        return self.field.clean(value)


class CharBlock(FieldBlock):
    def __init__(self, required=True, help_text=None, max_length=None, min_length=None, **kwargs):
        # CharField's 'label' and 'initial' parameters are not exposed, as Block handles that functionality natively (via 'label' and 'default')
        self.field = forms.CharField(required=required, help_text=help_text, max_length=max_length, min_length=min_length)
        super(CharBlock, self).__init__(**kwargs)

    def get_searchable_content(self, value):
        return [force_text(value)]


class URLBlock(FieldBlock):
    def __init__(self, required=True, help_text=None, max_length=None, min_length=None, **kwargs):
        self.field = forms.URLField(required=required, help_text=help_text, max_length=max_length, min_length=min_length)
        super(URLBlock, self).__init__(**kwargs)


class RichTextBlock(FieldBlock):
    @cached_property
    def field(self):
        from wagtail.wagtailcore.fields import RichTextArea
        return forms.CharField(widget=RichTextArea)

    def render_basic(self, value):
        return mark_safe('<div class="rich-text">' + expand_db_html(value) + '</div>')

    def get_searchable_content(self, value):
        return [force_text(value)]


class RawHTMLBlock(FieldBlock):
    def __init__(self, required=True, help_text=None, max_length=None, min_length=None, **kwargs):
        self.field = forms.CharField(
            required=required, help_text=help_text, max_length=max_length, min_length=min_length,
            widget = forms.Textarea)
        super(RawHTMLBlock, self).__init__(**kwargs)

    def render_basic(self, value):
        return mark_safe(value)  # if it isn't safe, that's the site admin's problem for allowing raw HTML blocks in the first place...

    class Meta:
        icon = 'code'


class ChooserBlock(FieldBlock):
    def __init__(self, required=True, **kwargs):
        self.required=required
        super(ChooserBlock, self).__init__(**kwargs)

    """Abstract superclass for fields that implement a chooser interface (page, image, snippet etc)"""
    @cached_property
    def field(self):
        return forms.ModelChoiceField(queryset=self.target_model.objects.all(), widget=self.widget, required=self.required)

    def to_python(self, value):
        if value is None or isinstance(value, self.target_model):
            return value
        else:
            try:
                return self.target_model.objects.get(pk=value)
            except self.target_model.DoesNotExist:
                return None

    def get_prep_value(self, value):
        if isinstance(value, self.target_model):
            return value.id
        else:
            return value

    def clean(self, value):
        # ChooserBlock works natively with model instances as its 'value' type (because that's what you
        # want to work with when doing front-end templating), but ModelChoiceField.clean expects an ID
        # as the input value (and returns a model instance as the result). We don't want to bypass
        # ModelChoiceField.clean entirely (it might be doing relevant validation, such as checking page
        # type) so we convert our instance back to an ID here. It means we have a wasted round-trip to
        # the database when ModelChoiceField.clean promptly does its own lookup, but there's no easy way
        # around that...
        if isinstance(value, self.target_model):
            value = value.pk
        return super(ChooserBlock, self).clean(value)

class PageChooserBlock(ChooserBlock):
    @cached_property
    def target_model(self):
        from wagtail.wagtailcore.models import Page  # TODO: allow limiting to specific page types
        return Page

    @cached_property
    def widget(self):
        from wagtail.wagtailadmin.widgets import AdminPageChooser
        return AdminPageChooser

    def render_basic(self, value):
        if value:
            return format_html('<a href="{0}">{1}</a>', value.url, value.title)
        else:
            return ''


# ===========
# StructBlock
# ===========

class BaseStructBlock(Block):
    class Meta:
        default = {}
        template = "wagtailadmin/blocks/struct.html"

    def __init__(self, local_blocks=None, **kwargs):
        self._constructor_kwargs = kwargs

        super(BaseStructBlock, self).__init__(**kwargs)

        self.child_blocks = self.base_blocks.copy()  # create a local (shallow) copy of base_blocks so that it can be supplemented by local_blocks
        if local_blocks:
            for name, block in local_blocks:
                block.set_name(name)
                self.child_blocks[name] = block

        self.child_js_initializers = {}
        for name, block in self.child_blocks.items():
            js_initializer = block.js_initializer()
            if js_initializer is not None:
                self.child_js_initializers[name] = js_initializer

        self.dependencies = self.child_blocks.values()

    def js_initializer(self):
        # skip JS setup entirely if no children have js_initializers
        if not self.child_js_initializers:
            return None

        return "StructBlock(%s)" % js_dict(self.child_js_initializers)

    @property
    def media(self):
        return forms.Media(js=['wagtailadmin/js/blocks/struct.js'])

    def render_form(self, value, prefix='', errors=None):
        if errors:
            if len(errors) > 1:
                # We rely on StructBlock.clean throwing a single ValidationError with a specially crafted
                # 'params' attribute that we can pull apart and distribute to the child blocks
                raise TypeError('StructBlock.render_form unexpectedly received multiple errors')
            error_dict = errors.as_data()[0].params
        else:
            error_dict = {}

        child_renderings = [
            block.render_form(value.get(name, block.meta.default), prefix="%s-%s" % (prefix, name),
                errors=error_dict.get(name))
            for name, block in self.child_blocks.items()
        ]

        list_items = format_html_join('\n', "<li>{0}</li>", [
            [child_rendering]
            for child_rendering in child_renderings
        ])

       
        # Can these be rendered with a template?
        if self.label:
            return format_html('<div class="struct-block"><label>{0}</label> <ul>{1}</ul></div>', self.label, list_items)
        else:
            return format_html('<div class="struct-block"><ul>{0}</ul></div>', list_items)

    def value_from_datadict(self, data, files, prefix):
        return dict([
            (name, block.value_from_datadict(data, files, '%s-%s' % (prefix, name)))
            for name, block in self.child_blocks.items()
        ])

    def clean(self, value):
        result = {}
        errors = {}
        for name, val in value.items():
            try:
                result[name] = self.child_blocks[name].clean(val)
            except ValidationError as e:
                errors[name] = ErrorList([e])

        if errors:
            # The message here is arbitrary - StructBlock.render_form will suppress it
            # and delegate the errors contained in the 'params' dict to the child blocks instead
            raise ValidationError('Validation error in StructBlock', params=errors)

        return result

    def to_python(self, value):
        # recursively call to_python on children and return as a StructValue
        return StructValue(self, [
            (
                name,
                child_block.to_python(value.get(name, child_block.meta.default))
            )
            for name, child_block in self.child_blocks.items()
        ])

    def get_prep_value(self, value):
        # recursively call get_prep_value on children and return as a plain dict
        return dict([
            (name, self.child_blocks[name].get_prep_value(val))
            for name, val in value.items()
        ])

    def get_searchable_content(self, value):
        content = []

        for name, block in self.child_blocks.items():
            content.extend(block.get_searchable_content(value.get(name, block.meta.default)))

        return content

    def deconstruct(self):
        """
        Always deconstruct StructBlock instances as if they were plain StructBlocks with all of the
        field definitions passed to the constructor - even if in reality this is a subclass of StructBlock
        with the fields defined declaratively, or some combination of the two.

        This ensures that the field definitions get frozen into migrations, rather than leaving a reference
        to a custom subclass in the user's models.py that may or may not stick around.
        """
        path = 'wagtail.wagtailcore.blocks.StructBlock'
        args = [self.child_blocks.items()]
        kwargs = self._constructor_kwargs
        return (path, args, kwargs)


@python_2_unicode_compatible  # provide equivalent __unicode__ and __str__ methods on Py2
class StructValue(collections.OrderedDict):
    def __init__(self, block, *args):
        super(StructValue, self).__init__(*args)
        self.block = block

    def __str__(self):
        return self.block.render(self)

    @cached_property
    def bound_blocks(self):
        return collections.OrderedDict([
            (name, block.bind(self.get(name)))
            for name, block in self.block.child_blocks.items()
        ])


class DeclarativeSubBlocksMetaclass(BaseBlock):
    """
    Metaclass that collects sub-blocks declared on the base classes.
    (cheerfully stolen from https://github.com/django/django/blob/master/django/forms/forms.py)
    """
    def __new__(mcs, name, bases, attrs):
        # Collect sub-blocks from current class.
        current_blocks = []
        for key, value in list(attrs.items()):
            if isinstance(value, Block):
                current_blocks.append((key, value))
                value.set_name(key)
                attrs.pop(key)
        current_blocks.sort(key=lambda x: x[1].creation_counter)
        attrs['declared_blocks'] = collections.OrderedDict(current_blocks)

        new_class = (super(DeclarativeSubBlocksMetaclass, mcs)
            .__new__(mcs, name, bases, attrs))

        # Walk through the MRO.
        declared_blocks = collections.OrderedDict()
        for base in reversed(new_class.__mro__):
            # Collect sub-blocks from base class.
            if hasattr(base, 'declared_blocks'):
                declared_blocks.update(base.declared_blocks)

            # Field shadowing.
            for attr, value in base.__dict__.items():
                if value is None and attr in declared_blocks:
                    declared_blocks.pop(attr)

        new_class.base_blocks = declared_blocks
        new_class.declared_blocks = declared_blocks

        return new_class

class StructBlock(six.with_metaclass(DeclarativeSubBlocksMetaclass, BaseStructBlock)):
    pass


# =========
# ListBlock
# =========

class ListBlock(Block):
    class Meta:
        # Default to a list consisting of one empty child item (using None to trigger the child's empty / default rendering)
        default = [None]

    def __init__(self, child_block, **kwargs):
        super(ListBlock, self).__init__(**kwargs)

        if isinstance(child_block, type):
            # child_block was passed as a class, so convert it to a block instance
            self.child_block = child_block()
        else:
            self.child_block = child_block

        self.dependencies = [self.child_block]
        self.child_js_initializer = self.child_block.js_initializer()

    @property
    def media(self):
        return forms.Media(js=['wagtailadmin/js/blocks/sequence.js', 'wagtailadmin/js/blocks/list.js'])

    def render_list_member(self, value, prefix, index, errors=None):
        """
        Render the HTML for a single list item in the form. This consists of an <li> wrapper, hidden fields
        to manage ID/deleted state, delete/reorder buttons, and the child block's own form HTML.
        """
        child = self.child_block.bind(value, prefix="%s-value" % prefix, errors=errors)
        return render_to_string('wagtailadmin/block_forms/list_member.html', {
            'prefix': prefix,
            'child': child,
            'index': index,
        })

    def html_declarations(self):
        # generate the HTML to be used when adding a new item to the list;
        # this is the output of render_list_member as rendered with the prefix '__PREFIX__'
        # (to be replaced dynamically when adding the new item) and the child block's default value
        # as its value.
        list_member_html = self.render_list_member(self.child_block.meta.default, '__PREFIX__', '')

        return format_html(
            '<script type="text/template" id="{0}-newmember">{1}</script>',
            self.definition_prefix, mark_safe(escape_script(list_member_html))
        )

    def js_initializer(self):
        opts = {'definitionPrefix': "'%s'" % self.definition_prefix}

        if self.child_js_initializer:
            opts['childInitializer'] = self.child_js_initializer

        return "ListBlock(%s)" % js_dict(opts)

    def render_form(self, value, prefix='', errors=None):
        if errors:
            if len(errors) > 1:
                # We rely on ListBlock.clean throwing a single ValidationError with a specially crafted
                # 'params' attribute that we can pull apart and distribute to the child blocks
                raise TypeError('ListBlock.render_form unexpectedly received multiple errors')
            error_list = errors.as_data()[0].params
        else:
            error_list = None

        list_members_html = [
            self.render_list_member(child_val, "%s-%d" % (prefix, i), i,
                errors=error_list[i] if error_list else None)
            for (i, child_val) in enumerate(value)
        ]

        return render_to_string('wagtailadmin/block_forms/list.html', {
            'label': self.label,
            'prefix': prefix,
            'list_members_html': list_members_html,
        })

    def value_from_datadict(self, data, files, prefix):
        count = int(data['%s-count' % prefix])
        values_with_indexes = []
        for i in range(0, count):
            if data['%s-%d-deleted' % (prefix, i)]:
                continue
            values_with_indexes.append(
                (
                    data['%s-%d-order' % (prefix, i)],
                    self.child_block.value_from_datadict(data, files, '%s-%d-value' % (prefix, i))
                )
            )

        values_with_indexes.sort()
        return [v for (i, v) in values_with_indexes]

    def clean(self, value):
        result = []
        errors = []
        for child_val in value:
            try:
                result.append(self.child_block.clean(child_val))
            except ValidationError as e:
                errors.append(ErrorList([e]))
            else:
                errors.append(None)

        if any(errors):
            # The message here is arbitrary - outputting error messages is delegated to the child blocks,
            # which only involves the 'params' list
            raise ValidationError('Validation error in ListBlock', params=errors)

        return result

    def to_python(self, value):
        # recursively call to_python on children and return as a list
        return [
            self.child_block.to_python(item)
            for item in value
        ]

    def get_prep_value(self, value):
        # recursively call get_prep_value on children and return as a list
        return [
            self.child_block.get_prep_value(item)
            for item in value
        ]

    def render_basic(self, value):
        children = format_html_join('\n', '<li>{0}</li>',
            [(self.child_block.render(child_value),) for child_value in value]
        )
        return format_html("<ul>{0}</ul>", children)

    def get_searchable_content(self, value):
        content = []

        for child_value in value:
            content.extend(self.child_block.get_searchable_content(child_value))

        return content


# ===========
# StreamBlock
# ===========

class BaseStreamBlock(Block):
    # TODO: decide what it means to pass a 'default' arg to StreamBlock's constructor. Logically we want it to be
    # of type StreamValue, but we can't construct one of those because it needs a reference back to the StreamBlock
    # that we haven't constructed yet...
    class Meta:
        @property
        def default(self):
            return StreamValue(self, [])

    def __init__(self, local_blocks=None, **kwargs):
        self._constructor_kwargs = kwargs

        super(BaseStreamBlock, self).__init__(**kwargs)

        self.child_blocks = self.base_blocks.copy()  # create a local (shallow) copy of base_blocks so that it can be supplemented by local_blocks
        if local_blocks:
            for name, block in local_blocks:
                block.set_name(name)
                self.child_blocks[name] = block

        self.dependencies = self.child_blocks.values()

    def render_list_member(self, block_type_name, value, prefix, index, errors=None):
        """
        Render the HTML for a single list item. This consists of an <li> wrapper, hidden fields
        to manage ID/deleted state/type, delete/reorder buttons, and the child block's own HTML.
        """
        child_block = self.child_blocks[block_type_name]
        child = child_block.bind(value, prefix="%s-value" % prefix, errors=errors)
        return render_to_string('wagtailadmin/block_forms/stream_member.html', {
            'child_blocks': self.child_blocks.values(),
            'block_type_name': block_type_name,
            'prefix': prefix,
            'child': child,
            'index': index,
        })

    def html_declarations(self):
        return format_html_join(
            '\n', '<script type="text/template" id="{0}-newmember-{1}">{2}</script>',
            [
                (
                    self.definition_prefix,
                    name,
                    mark_safe(escape_script(self.render_list_member(name, child_block.meta.default, '__PREFIX__', '')))
                )
                for name, child_block in self.child_blocks.items()
            ]
        )

    @property
    def media(self):
        return forms.Media(js=['wagtailadmin/js/blocks/sequence.js', 'wagtailadmin/js/blocks/stream.js'])

    def js_initializer(self):
        # compile a list of info dictionaries, one for each available block type
        child_blocks = []
        for name, child_block in self.child_blocks.items():
            # each info dictionary specifies at least a block name
            child_block_info = {'name': "'%s'" % name}

            # if the child defines a JS initializer function, include that in the info dict
            # along with the param that needs to be passed to it for initializing an empty/default block
            # of that type
            child_js_initializer = child_block.js_initializer()
            if child_js_initializer:
                child_block_info['initializer'] = child_js_initializer

            child_blocks.append(indent(js_dict(child_block_info)))

        opts = {
            'definitionPrefix': "'%s'" % self.definition_prefix,
            'childBlocks': '[\n%s\n]' % ',\n'.join(child_blocks),
        }

        return "StreamBlock(%s)" % js_dict(opts)

    def render_form(self, value, prefix='', errors=None):
        if errors:
            if len(errors) > 1:
                # We rely on ListBlock.clean throwing a single ValidationError with a specially crafted
                # 'params' attribute that we can pull apart and distribute to the child blocks
                raise TypeError('ListBlock.render_form unexpectedly received multiple errors')
            error_list = errors.as_data()[0].params
        else:
            error_list = None

        # drop any child values that are an unrecognised block type
        valid_children = [child for child in value if child.block_type in self.child_blocks]

        list_members_html = [
            self.render_list_member(child.block_type, child.value, "%s-%d" % (prefix, i), i,
                errors=error_list[i] if error_list else None)
            for (i, child) in enumerate(valid_children)
        ]

        return render_to_string('wagtailadmin/block_forms/stream.html', {
            'label': self.label,
            'prefix': prefix,
            'list_members_html': list_members_html,
            'child_blocks': self.child_blocks.values(),
            'header_menu_prefix': '%s-before' % prefix,
        })

    def value_from_datadict(self, data, files, prefix):
        count = int(data['%s-count' % prefix])
        values_with_indexes = []
        for i in range(0, count):
            if data['%s-%d-deleted' % (prefix, i)]:
                continue
            block_type_name = data['%s-%d-type' % (prefix, i)]
            try:
                child_block = self.child_blocks[block_type_name]
            except KeyError:
                continue

            values_with_indexes.append(
                (
                    int(data['%s-%d-order' % (prefix, i)]),
                    block_type_name,
                    child_block.value_from_datadict(data, files, '%s-%d-value' % (prefix, i)),
                )
            )

        values_with_indexes.sort()
        return StreamValue(self, [
            (block_type_name, value)
            for (index, block_type_name, value) in values_with_indexes
        ])

    def clean(self, value):
        cleaned_data = []
        errors = []
        for child in value:  # child is a BoundBlock instance
            try:
                cleaned_data.append(
                    (child.block.name, child.block.clean(child.value))
                )
            except ValidationError as e:
                errors.append(ErrorList([e]))
            else:
                errors.append(None)

        if any(errors):
            # The message here is arbitrary - outputting error messages is delegated to the child blocks,
            # which only involves the 'params' list
            raise ValidationError('Validation error in StreamBlock', params=errors)

        return StreamValue(self, cleaned_data)

    def to_python(self, value):
        # the incoming JSONish representation is a list of dicts, each with a 'type' and 'value' field.
        # Convert this to a StreamValue backed by a list of (type, value) tuples
        return StreamValue(self, [
            (child_data['type'], self.child_blocks[child_data['type']].to_python(child_data['value']))
            for child_data in value
            if child_data['type'] in self.child_blocks
        ])

    def get_prep_value(self, value):
        if value is None:
            return None

        return [
            {'type': child.block.name, 'value': child.block.get_prep_value(child.value)}
            for child in value  # child is a BoundBlock instance
        ]

    def render_basic(self, value):
        return format_html_join('\n', '<div class="block-{1}">{0}</div>',
            [(child, child.block_type) for child in value]
        )

    def get_searchable_content(self, value):
        content = []

        for child in value:
            content.extend(child.block.get_searchable_content(child.value))

        return content

    def deconstruct(self):
        """
        Always deconstruct StreamBlock instances as if they were plain StreamBlocks with all of the
        field definitions passed to the constructor - even if in reality this is a subclass of StreamBlock
        with the fields defined declaratively, or some combination of the two.

        This ensures that the field definitions get frozen into migrations, rather than leaving a reference
        to a custom subclass in the user's models.py that may or may not stick around.
        """
        path = 'wagtail.wagtailcore.blocks.StreamBlock'
        args = [self.child_blocks.items()]
        kwargs = self._constructor_kwargs
        return (path, args, kwargs)


class StreamBlock(six.with_metaclass(DeclarativeSubBlocksMetaclass, BaseStreamBlock)):
    pass


@python_2_unicode_compatible  # provide equivalent __unicode__ and __str__ methods on Py2
class StreamValue(collections.Sequence):
    """
    Custom type used to represent the value of a StreamBlock; behaves as a sequence of BoundBlocks
    (which keep track of block types in a way that the values alone wouldn't).
    """

    @python_2_unicode_compatible
    class StreamChild(BoundBlock):
        """Provides some extensions to BoundBlock to make it more natural to work with on front-end templates"""
        def __str__(self):
            """Render the value according to the block's native rendering"""
            return self.block.render(self.value)

        @property
        def block_type(self):
            """
            Syntactic sugar so that we can say child.block_type instead of child.block.name.
            (This doesn't belong on BoundBlock itself because the idea of block.name denoting
            the child's "type" ('heading', 'paragraph' etc) is unique to StreamBlock, and in the
            wider context people are liable to confuse it with the block class (CharBlock etc).
            """
            return self.block.name

    def __init__(self, stream_block, stream_data):
        self.stream_block = stream_block  # the StreamBlock object that handles this value
        self.stream_data = stream_data  # a list of (type_name, value) tuples
        self._bound_blocks = {}  # populated lazily from stream_data as we access items through __getitem__

    def __getitem__(self, i):
        if i not in self._bound_blocks:
            type_name, value = self.stream_data[i]
            child_block = self.stream_block.child_blocks[type_name]
            self._bound_blocks[i] = StreamValue.StreamChild(child_block, value)

        return self._bound_blocks[i]

    def __len__(self):
        return len(self.stream_data)

    def __repr__(self):
        return repr(list(self))

    def __str__(self):
        return self.stream_block.render(self)


# ========================
# django.forms integration
# ========================

class BlockWidget(forms.Widget):
    """Wraps a block object as a widget so that it can be incorporated into a Django form"""
    def __init__(self, block_def, attrs=None):
        super(BlockWidget, self).__init__(attrs=attrs)
        self.block_def = block_def

    def render_with_errors(self, name, value, attrs=None, errors=None):
        bound_block = self.block_def.bind(value, prefix=name, errors=errors)
        js_initializer = self.block_def.js_initializer()
        if js_initializer:
            js_snippet = """
                <script>
                $(function() {
                    var initializer = %s;
                    initializer('%s');
                })
                </script>
            """ % (js_initializer, name)
        else:
            js_snippet = ''
        return mark_safe(bound_block.render_form() + js_snippet)

    def render(self, name, value, attrs=None):
        return self.render_with_errors(name, value, attrs=attrs, errors=None)

    @property
    def media(self):
        return self.block_def.all_media()

    def value_from_datadict(self, data, files, name):
        return self.block_def.value_from_datadict(data, files, name)


class BlockField(forms.Field):
    """Wraps a block object as a form field so that it can be incorporated into a Django form"""
    def __init__(self, block=None, **kwargs):
        if block is None:
            raise ImproperlyConfigured("BlockField was not passed a 'block' object")
        self.block = block

        if 'widget' not in kwargs:
            kwargs['widget'] = BlockWidget(block)

        super(BlockField, self).__init__(**kwargs)

    def clean(self, value):
        return self.block.clean(value)
