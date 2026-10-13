mod adapter;
mod net;
mod scene;
mod ui;
mod world_map;

use bevy::prelude::*;

use adapter::NpcDirector;
use net::{ClientConfig, NetBridge, WorldSnapshot};
use scene::SceneState;
use ui::{NearbyInteract, UiState};
use world_map::WorldMapState;

fn main() {
    let config = ClientConfig::from_args();
    let net = NetBridge::new(&config.server);

    App::new()
        .insert_resource(config)
        .insert_resource(net)
        .insert_resource(WorldSnapshot::offline())
        .insert_resource(SceneState::default())
        .insert_resource(NpcDirector::default())
        .insert_resource(NearbyInteract::default())
        .insert_resource(UiState::default())
        .insert_resource(WorldMapState::default())
        .add_plugins(DefaultPlugins.set(WindowPlugin {
            primary_window: Some(Window {
                title: "AITown3 Bevy Client".to_string(),
                resolution: (1280, 760).into(),
                ..default()
            }),
            ..default()
        }))
        .add_systems(Startup, (scene::setup_scene, ui::setup_ui))
        .add_systems(
            Update,
            (
                net::schedule_state_sync,
                net::receive_network_messages,
                world_map::drive_travel,
                scene::render_scene,
                scene::place_player_in_room,
                adapter::sync_npcs,
                adapter::run_npc_behaviour,
                scene::player_input,
                ui::detect_nearby,
                ui::interaction_input,
            )
                .chain(),
        )
        .add_systems(
            Update,
            (
                adapter::update_npc_bubbles,
                scene::draw_room_gizmos,
                world_map::render_world_map,
                world_map::draw_world_map_gizmos,
                ui::update_status,
                ui::update_clock,
                ui::update_history_panel,
                ui::poll_history,
                ui::history_wheel,
                ui::update_prompt,
                ui::update_toast,
                ui::update_menu_panel,
                ui::update_dialogue_panel,
                ui::update_debug_hud,
                ui::update_night_overlay,
            )
                .after(ui::interaction_input),
        )
        .run();
}
