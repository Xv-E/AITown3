mod adapter;
mod icons;
mod net;
mod scene;
mod speech;
mod ui;
mod world_map;

use bevy::prelude::*;

use adapter::NpcDirector;
use net::{ClientConfig, NetBridge, SimClock, WorldSnapshot};
use scene::{PlayerSync, SceneState, WalkGrid};
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
        .insert_resource(WalkGrid::default())
        .insert_resource(PlayerSync::default())
        .insert_resource(speech::SpeechPlayer::default())
        .insert_resource(SimClock::default())
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
        .add_systems(Startup, (icons::setup_icons, icons::use_cjk_font, scene::setup_scene, ui::setup_ui))
        .add_systems(
            Update,
            (
                net::schedule_state_sync,
                net::receive_network_messages,
                speech::update_speech,
                ui::ui_buttons,
                ui::chat_typing,
                ui::npc_chat_requests,
                scene::rebuild_walk_grid,
                scene::render_scene,
                scene::sync_player_from_server,
                adapter::sync_npcs,
                adapter::run_npc_behaviour,
                scene::player_input,
                scene::report_player_position,
                scene::camera_follow,
                ui::detect_nearby,
                ui::interaction_input,
            )
                .chain(),
        )
        .add_systems(
            Update,
            (
                speech::render_speech_bubbles,
                adapter::update_npc_bubbles,
                scene::draw_world_gizmos,
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
                ui::update_toolbar,
                ui::button_hover,
                ui::update_dialogue_panel,
                ui::update_debug_hud,
                ui::update_night_overlay,
            )
                .after(ui::interaction_input),
        )
        .run();
}
