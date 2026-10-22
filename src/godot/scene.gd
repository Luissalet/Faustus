extends SceneTree

var request: Dictionary
var failure := ""

func _initialize() -> void:
	call_deferred("perform")

func finish(value: Dictionary) -> void:
	value["engine_version"] = Engine.get_version_info().string
	var pending: String = request.result_file + ".pending"
	var file := FileAccess.open(pending, FileAccess.WRITE)
	if file == null:
		push_error("Cannot retain operation result")
		quit(1)
		return
	file.store_string(JSON.stringify(value))
	file.close()
	if DirAccess.rename_absolute(pending, request.result_file) != OK:
		push_error("Cannot commit operation result")
		quit(1)
		return
	print("FAUSTUS_GODOT:" + JSON.stringify(value))
	quit(0 if value.get("ok", false) else 1)

func decode(value: Variant, expected: int) -> Variant:
	if value is Dictionary and value.has("$type"):
		var kind: String = value["$type"]
		var v: Variant = value.get("value")
		match kind:
			"Vector2", "Vector3", "Vector4", "Color":
				var count := 2 if kind == "Vector2" else 3 if kind == "Vector3" else 4
				if not v is Array or v.size() != count:
					failure = "Wrong number of components for " + kind
					return null
				for n in v:
					if not n is float and not n is int:
						failure = "Components must be numeric"
						return null
				match kind:
					"Vector2": return Vector2(v[0], v[1])
					"Vector3": return Vector3(v[0], v[1], v[2])
					"Vector4": return Vector4(v[0], v[1], v[2], v[3])
					"Color": return Color(v[0], v[1], v[2], v[3])
			"NodePath": return NodePath(str(v))
			"StringName": return StringName(str(v))
			"Resource":
				var path: String = value.get("path", "")
				if not path.begins_with("res://") or not ResourceLoader.exists(path):
					failure = "Resource does not exist in project: " + path
					return null
				return load(path)
			_:
				failure = "Unsupported value type: " + kind
				return null
	if expected == TYPE_INT and (value is int or value is float) and value == floor(value):
		return int(value)
	if expected == TYPE_FLOAT and (value is int or value is float):
		return float(value)
	if expected == TYPE_STRING_NAME and value is String:
		return StringName(value)
	return value

func encode(value: Variant) -> Variant:
	if value == null: return null
	if value is Vector2:
		return {"$type":"Vector2", "value":[value.x, value.y]}
	if value is Vector3:
		return {"$type":"Vector3", "value":[value.x, value.y, value.z]}
	if value is Vector4:
		return {"$type":"Vector4", "value":[value.x, value.y, value.z, value.w]}
	if value is Color:
		return {"$type":"Color", "value":[value.r, value.g, value.b, value.a]}
	if value is Resource:
		return {"$type":"Resource", "path":value.resource_path, "class":value.get_class()}
	if value is NodePath or value is StringName:
		return str(value)
	if value is Object:
		return {"class":value.get_class()}
	return value

func properties(object: Object) -> Array:
	var values: Array = []
	for item in object.get_property_list():
		var fields: Array = request.arguments.get("fields", [])
		if not fields.is_empty() and not item.name in fields: continue
		if item.usage & PROPERTY_USAGE_STORAGE or not fields.is_empty():
			values.append({"name":item.name, "type":type_string(item.type), "value":encode(object.get(item.name))})
	return values

func set_values(object: Object, values: Dictionary) -> bool:
	var available: Dictionary = {}
	for item in object.get_property_list():
		available[item.name] = item
	var converted: Dictionary = {}
	for key in values:
		if not available.has(key) or key in ["script", "owner", "scene_file_path"]:
			failure = "Unknown or structural property; use explicit operation: " + key
			return false
		var item: Dictionary = available[key]
		var value: Variant = decode(values[key], item.type)
		if not failure.is_empty(): return false
		if value != null and item.type != TYPE_NIL and typeof(value) != item.type:
			failure = "Expected " + type_string(item.type) + " for " + key
			return false
		if value == null and item.type != TYPE_NIL and item.type != TYPE_OBJECT:
			failure = "Null is not valid for " + key
			return false
		converted[key] = value
	for key in converted: object.set(key, converted[key])
	return true

func make_node(kind: String, node_name: String) -> Node:
	if not ClassDB.class_exists(kind) or not ClassDB.can_instantiate(kind) or not ClassDB.is_parent_class(kind, "Node"):
		failure = "Not an instantiable Node class: " + kind
		return null
	var node: Node = ClassDB.instantiate(kind)
	node.name = node_name
	if str(node.name) != node_name or node_name.is_empty() or node_name in [".", ".."]:
		node.free()
		failure = "Invalid node name"
		return null
	return node

func describe(root: Node, node: Node, result: Array) -> void:
	result.append({"path":str(root.get_path_to(node)), "name":str(node.name), "class":node.get_class(), "properties":properties(node)})
	for child in node.get_children(): describe(root, child, result)

func perform() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() != 1:
		push_error("Operation request file required")
		quit(1)
		return
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(args[0]))
	if not parsed is Dictionary:
		push_error("Invalid operation request")
		quit(1)
		return
	request = parsed
	var operation: String = request.operation
	var spec: Dictionary = request.arguments
	if operation == "class_properties":
		var kind: String = spec.get("class", "Node2D")
		if not ClassDB.class_exists(kind):
			finish({"ok":false, "error":"Unknown class: " + kind})
			return
		var result: Array = []
		for item in ClassDB.class_get_property_list(kind):
			result.append({"name":item.name, "type":type_string(item.type), "hint":item.hint_string})
		finish({"ok":true, "class":kind, "properties":result})
		return
	var root: Node
	var original_uid := ResourceUID.text_to_id(request.get("uid", "")) if not request.get("uid", "").is_empty() else -1
	if operation == "create_scene":
		root = make_node(spec.get("class", "Node2D"), spec.get("name", "Scene"))
	else:
		var scene: PackedScene = ResourceLoader.load(request.scene, "PackedScene", ResourceLoader.CACHE_MODE_IGNORE)
		if scene == null:
			finish({"ok":false, "error":"Scene cannot be loaded"})
			return
		if original_uid < 0: original_uid = ResourceLoader.get_resource_uid(request.scene)
		root = scene.instantiate(PackedScene.GEN_EDIT_STATE_INSTANCE)
	if root == null:
		finish({"ok":false, "error":failure if not failure.is_empty() else "Scene cannot be instantiated"})
		return
	var target: Node = root if operation == "create_scene" else root.get_node_or_null(NodePath(spec.get("node", ".")))
	if target == null:
		failure = "Node path not found"
	elif operation == "add_node":
		var child := make_node(spec.get("class", "Node2D"), spec.get("name", "Node"))
		if child != null:
			if target.get_node_or_null(NodePath(str(child.name))) != null:
				failure = "Child already exists"
				child.free()
			else:
				target.add_child(child)
				child.owner = root
				set_values(child, spec.get("properties", {}))
				target = child
	elif operation in ["set_properties", "create_scene"]:
		set_values(target, spec.get("properties", {}))
	elif operation == "attach_script":
		var script_path: String = spec.get("script", "")
		var script: Script = load(script_path) if script_path.begins_with("res://") else null
		if script == null or not script.can_instantiate():
			failure = "Script is missing or has parse errors"
		else:
			target.set_script(script)
	if not failure.is_empty():
		root.free()
		finish({"ok":false, "error":failure})
		return
	var nodes: Array = []
	describe(root, root, nodes)
	if operation != "inspect_scene":
		var packed := PackedScene.new()
		var error := packed.pack(root)
		if error == OK: error = ResourceSaver.save(packed, request.staged)
		if original_uid < 0: original_uid = ResourceUID.create_id()
		if error == OK: error = ResourceSaver.set_uid(request.staged, original_uid)
		if error != OK:
			root.free()
			finish({"ok":false, "error":"Scene save failed: " + error_string(error)})
			return
		# Verify the saved resource rather than reporting an in-memory setter as
		# persisted. Runtime-only fields otherwise silently disappear on reopen.
		var verify_scene: PackedScene = ResourceLoader.load(request.staged, "PackedScene", ResourceLoader.CACHE_MODE_IGNORE)
		var reopened: Node = verify_scene.instantiate(PackedScene.GEN_EDIT_STATE_INSTANCE) if verify_scene != null else null
		var reopened_target: Node = reopened.get_node_or_null(root.get_path_to(target)) if reopened != null else null
		if reopened_target == null:
			failure = "Saved scene did not retain the edited node"
		else:
			var keys: Array = spec.get("properties", {}).keys()
			if operation == "attach_script": keys.append("script")
			for key in keys:
				if JSON.stringify(encode(reopened_target.get(key))) != JSON.stringify(encode(target.get(key))):
					failure = "Property is not retained in the saved scene: " + key
		if reopened != null: reopened.free()
		if not failure.is_empty():
			root.free()
			finish({"ok":false, "error":failure})
			return
	root.free()
	finish({"ok":true, "scene":request.scene, "nodes":nodes, "uid":ResourceUID.id_to_text(original_uid) if original_uid >= 0 else ""})
