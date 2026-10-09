import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {validateConfig, assignmentFor, monitorFor} from '../extensions/favorite-placement@rpembry.github.io/placement-core.js';
const config = {version:1, monitors:{right:{connector:'DP-9',serial:'example-serial'}},
    assignments:[{monitor:'right',classes:['crx_example'],title:'Example'}]};
test('exact identities and optional terminal title avoid moving other apps', () => {
    validateConfig(config);
    assert.equal(assignmentFor(config,{classes:['crx_example-other'],title:'Example'}),null);
    assert.equal(assignmentFor(config,{classes:['crx_example'],title:'Other'}),null);
    assert.equal(assignmentFor(config,{classes:['crx_example'],title:'Example'}).monitor,'right');
});
test('physical monitor survives index reorder and fails closed on replacement', () => {
    assert.equal(monitorFor(config.monitors.right,[{index:0,connector:'DP-9',serial:'other'}]),null);
    assert.equal(monitorFor(config.monitors.right,[{index:2,connector:'DP-9',serial:'example-serial'}]),2);
    assert.equal(monitorFor(config.monitors.right,[{index:0,connector:'DP-9',serial:'example-serial'},
        {index:1,connector:'DP-9',serial:'example-serial'}]),null);
});
test('overlapping class assignments are rejected', () => {
    assert.throws(() => validateConfig({...config,assignments:[...config.assignments,...config.assignments]}));
});

test('new window identity is retried once, manual moves stay put, disable cancels timers', () => {
    const callbacks = new Map();
    const moves = [];
    let seq = 0, created;
    const window = {get_window_type:()=>1,get_wm_class:()=> 'crx_example',
        get_wm_class_instance:()=>null,get_gtk_application_id:()=>null,
        get_title:()=> 'Example',get_monitor:()=>0,move_to_monitor:i=>moves.push(i)};
    const source = readFileSync(new URL('../extensions/favorite-placement@rpembry.github.io/extension.js',import.meta.url),'utf8')
        .replace(/^import .*;\n/gm,'').replace('export default class FavoritePlacement','class FavoritePlacement')+
        '\nglobalThis.Placement = FavoritePlacement;';
    const context = {validateConfig,assignmentFor,monitorFor,TextDecoder,console,
        Extension:class {},Meta:{WindowType:{NORMAL:1}},
        Main:{sessionMode:{currentMode:'user'},screenShield:{locked:false}},
        Gio:{File:{new_for_path:()=>({load_contents:()=>[true,new TextEncoder().encode(JSON.stringify(config))]})},
            DBus:{session:{}},DBusExportedObject:{wrapJSObject:()=>({export(){},unexport(){}})}},
        GLib:{get_user_config_dir:()=>'/example',build_filenamev:xs=>xs.join('/'),PRIORITY_DEFAULT:0,
            SOURCE_REMOVE:0,timeout_add:(_priority,_delay,cb)=>{callbacks.set(++seq,cb);return seq;},
            source_remove:id=>callbacks.delete(id)},
        global:{backend:{get_monitor_manager:()=>({get_monitors:()=>[
            {get_connector:()=> 'DP-9',get_serial:()=> 'example-serial'}],get_monitor_for_connector:()=>2})},
            display:{connect:(_signal,cb)=>{created=cb;return 1;},disconnect(){},list_all_windows:()=>[window]}}};
    runInNewContext(source,context);
    const placement = new context.Placement();
    placement.enable();
    assert.deepEqual(moves,[]);
    created(null,window);
    for (const [id,cb] of [...callbacks.entries()]) { cb(); callbacks.delete(id); }
    assert.deepEqual(moves,[2]);
    placement._queue(window);
    placement.disable();
    assert.equal(callbacks.size,0);
});
