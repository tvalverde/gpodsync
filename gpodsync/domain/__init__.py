"""Logic with no Django in it.

Everything worth testing lives here, and the absence of framework imports is what
makes the 100% branch-coverage gate on this package both reachable and meaningful.
When the gate starts feeling impossible, the usual cause is logic that has leaked
out into a view.
"""
