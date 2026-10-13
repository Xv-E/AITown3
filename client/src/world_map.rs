//! World map overlay (M): zones at their map coordinates, routes between them,
//! who is where. Pick a zone and the player walks there hop by hop.

use bevy::{math::Isometry2d, prelude::*};
use std::{collections::HashMap, time::Duration, time::Instant};

use crate::net::{NetBridge, WorldSnapshot};
use crate::ui::UiState;

const MAP_BOX: Vec2 = Vec2::new(900.0, 470.0);
const MAP_CENTER: Vec2 = Vec2::new(0.0, -30.0);
const MAP_Z: f32 = 40.0;
const HOP_RETRY: Duration = Duration::from_secs(3);

#[derive(Component)]
pub struct WorldMapEntity;

#[derive(Resource, Default)]
pub struct WorldMapState {
    key: Option<(u64, String)>,
}

/// Screen positions of every zone (zones without map coordinates go on a ring).
fn layout(world: &WorldSnapshot) -> Vec<(String, Vec2)> {
    let mut zones: Vec<_> = world.map.locations.iter().collect();
    zones.sort_by(|a, b| a.name.cmp(&b.name));
    let placed: Vec<(String, Vec2)> = zones
        .iter()
        .filter_map(|z| Some((z.name.clone(), Vec2::new(z.map_x?, z.map_y?))))
        .collect();
    let mut out = Vec::new();
    if !placed.is_empty() {
        let min = placed.iter().fold(Vec2::splat(f32::MAX), |m, (_, p)| m.min(*p));
        let max = placed.iter().fold(Vec2::splat(f32::MIN), |m, (_, p)| m.max(*p));
        let span = (max - min).max(Vec2::splat(1.0));
        let scale = (MAP_BOX.x / span.x).min(MAP_BOX.y / span.y);
        let mid = (min + max) * 0.5;
        for (name, p) in placed {
            out.push((name, MAP_CENTER + (p - mid) * scale));
        }
    }
    let unplaced: Vec<_> = zones
        .iter()
        .filter(|z| z.map_x.is_none() || z.map_y.is_none())
        .collect();
    let n = unplaced.len().max(1) as f32;
    for (i, z) in unplaced.iter().enumerate() {
        let a = i as f32 / n * std::f32::consts::TAU;
        out.push((z.name.clone(), MAP_CENTER + Vec2::new(a.cos(), a.sin()) * MAP_BOX * 0.45));
    }
    out
}

fn sorted_names(world: &WorldSnapshot) -> Vec<String> {
    let mut names: Vec<String> = world.map.locations.iter().map(|z| z.name.clone()).collect();
    names.sort();
    names
}

pub fn map_input(keys: &ButtonInput<KeyCode>, world: &WorldSnapshot, ui: &mut UiState) {
    if keys.just_pressed(KeyCode::Escape) || keys.just_pressed(KeyCode::KeyM) {
        ui.world_map = None;
        return;
    }
    let names = sorted_names(world);
    let Some(view) = ui.world_map.as_mut() else {
        return;
    };
    if names.is_empty() {
        return;
    }
    let step: isize = if keys.just_pressed(KeyCode::KeyD)
        || keys.just_pressed(KeyCode::ArrowRight)
        || keys.just_pressed(KeyCode::KeyS)
        || keys.just_pressed(KeyCode::ArrowDown)
    {
        1
    } else if keys.just_pressed(KeyCode::KeyA)
        || keys.just_pressed(KeyCode::ArrowLeft)
        || keys.just_pressed(KeyCode::KeyW)
        || keys.just_pressed(KeyCode::ArrowUp)
    {
        -1
    } else {
        0
    };
    if step != 0 {
        let idx = names.iter().position(|n| *n == view.selected).unwrap_or(0) as isize;
        view.selected = names[(idx + step).rem_euclid(names.len() as isize) as usize].clone();
    }
    if keys.just_pressed(KeyCode::Enter) || keys.just_pressed(KeyCode::KeyE) {
        let target = view.selected.clone();
        let here = world.current_location.name.clone();
        match world.map.route(&here, &target) {
            Some(route) if route.is_empty() => ui.toast(format!("You are already in {target}.")),
            Some(route) => {
                ui.toast(format!("Travelling to {target} via {}", route.join(" -> ")));
                ui.travel = route;
                ui.travel_sent = None;
                ui.world_map = None;
            }
            None => ui.toast(format!("No known route to {target}.")),
        }
    }
}

/// Sends one `move` per hop; waits for the state sync to show the arrival before the next.
pub fn drive_travel(world: Res<WorldSnapshot>, net: Res<NetBridge>, mut ui: ResMut<UiState>) {
    let Some(next) = ui.travel.first().cloned() else {
        return;
    };
    let here = world.current_location.name.clone();
    if here == next {
        ui.travel.remove(0);
        ui.travel_sent = None;
        if ui.travel.is_empty() {
            ui.toast(format!("Arrived in {here}."));
        }
        return;
    }
    if ui.travel_sent.is_some_and(|t| t.elapsed() < HOP_RETRY) {
        return;
    }
    if !world.adjacent.contains(&next) {
        ui.cancel_travel();
        ui.toast(format!("The way to {next} is blocked."));
        return;
    }
    let params = HashMap::from([
        ("c".to_string(), world.player.name.clone()),
        ("from".to_string(), here),
        ("to".to_string(), next),
    ]);
    net.execute("move", params);
    ui.travel_sent = Some(Instant::now());
}

pub fn render_world_map(
    mut commands: Commands,
    world: Res<WorldSnapshot>,
    ui: Res<UiState>,
    mut state: ResMut<WorldMapState>,
    existing: Query<Entity, With<WorldMapEntity>>,
) {
    let key = ui.world_map.as_ref().map(|v| (world.revision, v.selected.clone()));
    if key == state.key {
        return;
    }
    state.key = key;
    for entity in &existing {
        commands.entity(entity).despawn();
    }
    let Some(view) = ui.world_map.as_ref() else {
        return;
    };

    commands.spawn((
        Sprite {
            color: Color::srgba(0.03, 0.05, 0.07, 0.96),
            custom_size: Some(Vec2::new(1400.0, 900.0)),
            ..default()
        },
        Transform::from_xyz(0.0, 0.0, MAP_Z),
        WorldMapEntity,
    ));
    spawn_label(
        &mut commands,
        Vec2::new(0.0, 300.0),
        "WORLD MAP    [A/D] choose zone   [Enter] travel   [M/Esc] close",
        18.0,
        Color::srgb(0.98, 0.92, 0.70),
    );

    let here = world.current_location.name.as_str();
    for (name, pos) in layout(&world) {
        let zone = world.map.locations.iter().find(|z| z.name == name);
        let color = if name == here {
            Color::srgb(0.25, 0.72, 1.0)
        } else if name == view.selected {
            Color::srgb(0.98, 0.80, 0.30)
        } else {
            Color::srgb(0.55, 0.60, 0.66)
        };
        commands.spawn((
            Sprite {
                color,
                custom_size: Some(Vec2::splat(if name == view.selected { 30.0 } else { 22.0 })),
                ..default()
            },
            Transform::from_xyz(pos.x, pos.y, MAP_Z + 2.0),
            WorldMapEntity,
        ));
        let people = zone.map(|z| z.persons.join(", ")).unwrap_or_default();
        let marker = if name == here { "  (you are here)" } else { "" };
        let mut text = format!("{name}{marker}");
        if !people.is_empty() {
            text.push_str(&format!("\n{people}"));
        }
        spawn_label(&mut commands, pos + Vec2::new(0.0, -36.0), &text, 14.0, Color::srgb(0.92, 0.95, 0.92));
    }

    if let Some(zone) = world.map.locations.iter().find(|z| z.name == view.selected) {
        let route = world.map.route(here, &zone.name).map(|r| r.len()).unwrap_or(0);
        let desc = if zone.description.is_empty() { "" } else { zone.description.as_str() };
        spawn_label(
            &mut commands,
            Vec2::new(0.0, -300.0),
            &format!("{}  -  {} hop(s) away\n{}", zone.name, route, desc),
            15.0,
            Color::srgb(0.85, 0.88, 0.92),
        );
    }
}

fn spawn_label(commands: &mut Commands, pos: Vec2, text: &str, size: f32, color: Color) {
    commands.spawn((
        Text2d::new(text),
        TextFont {
            font_size: size,
            ..default()
        },
        TextColor(color),
        Transform::from_xyz(pos.x, pos.y, MAP_Z + 3.0),
        WorldMapEntity,
    ));
}

pub fn draw_world_map_gizmos(mut gizmos: Gizmos, world: Res<WorldSnapshot>, ui: Res<UiState>) {
    let Some(view) = ui.world_map.as_ref() else {
        return;
    };
    let positions: HashMap<String, Vec2> = layout(&world).into_iter().collect();
    for edge in &world.map.edges {
        if let (Some(a), Some(b)) = (positions.get(&edge.a), positions.get(&edge.b)) {
            gizmos.line_2d(*a, *b, Color::srgb(0.40, 0.46, 0.52));
        }
    }
    let here = world.current_location.name.as_str();
    if let Some(route) = world.map.route(here, &view.selected) {
        let mut prev = positions.get(here).copied();
        for hop in &route {
            let Some(next) = positions.get(hop).copied() else {
                break;
            };
            if let Some(p) = prev {
                gizmos.line_2d(p, next, Color::srgb(0.98, 0.80, 0.30));
            }
            prev = Some(next);
        }
    }
    if let Some(sel) = positions.get(&view.selected) {
        gizmos.circle_2d(Isometry2d::from_translation(*sel), 26.0, Color::srgb(0.98, 0.80, 0.30));
    }
}
