"""A page that iOS repacks because of a widget must still verify (widget-reflow-verify.patch).

On an iPhone Air (iOS 27.0.1) a write that added two apps to a page holding two small
widgets came back with the Loon widget two places later: SpringBoard moved the 2x2 widget
to keep the grid gapless. The apps and folders were in order, so the write was correct,
but ``same_layout`` compared order and the engine rolled a good layout back six times.
"""

import unittest

from ios_homescreen.state import same_layout
from ios_homescreen.workflow import _widget_reflow_only


def app(ident):
    return {"bundleIdentifier": ident, "displayIdentifier": ident}


def widget(ident, size="small"):
    return {
        "bundleIdentifier": ident,
        "containerBundleIdentifier": ident,
        "displayIdentifier": ident,
        "elementType": "widget",
        "gridSize": size,
        "iconType": "custom",
        "widgetIdentifier": ident,
    }


def folder(name, members):
    return {
        "displayName": name,
        "listType": "folder",
        "iconLists": [[app(ident) for ident in members]],
    }


DOCK = []


class WidgetReflowTest(unittest.TestCase):
    def test_widget_moved_within_the_page_matches(self):
        planned = [DOCK, [app("a"), app("b"), widget("loon"), app("c"), widget("door")]]
        read = [DOCK, [app("a"), app("b"), app("c"), widget("loon"), widget("door")]]
        self.assertFalse(same_layout(read, planned))
        self.assertTrue(_widget_reflow_only(read, planned))

    def test_app_order_still_matters(self):
        planned = [DOCK, [app("a"), widget("loon"), app("b")]]
        read = [DOCK, [app("b"), app("a"), widget("loon")]]
        self.assertFalse(_widget_reflow_only(read, planned))

    def test_missing_widget_is_not_a_match(self):
        planned = [DOCK, [app("a"), widget("loon"), app("b")]]
        read = [DOCK, [app("a"), app("b")]]
        self.assertFalse(_widget_reflow_only(read, planned))

    def test_widget_on_another_page_is_not_a_match(self):
        planned = [DOCK, [widget("loon"), app("a")], [app("b")]]
        read = [DOCK, [app("a")], [widget("loon"), app("b")]]
        self.assertFalse(_widget_reflow_only(read, planned))

    def test_folder_change_still_matters(self):
        planned = [DOCK, [folder("搞机", ["feather", "esign"]), app("a")]]
        read = [DOCK, [folder("工具", ["feather", "esign"]), app("a")]]
        self.assertFalse(_widget_reflow_only(read, planned))

    def test_widget_size_change_still_matters(self):
        planned = [DOCK, [widget("loon", "small"), app("a")]]
        read = [DOCK, [widget("loon", "medium"), app("a")]]
        self.assertFalse(_widget_reflow_only(read, planned))


if __name__ == "__main__":
    unittest.main()
