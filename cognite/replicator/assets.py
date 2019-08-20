import logging
import time
from typing import Dict, List

from cognite.client import CogniteClient
from cognite.client.data_classes.assets import Asset

from . import replication


def build_asset_create(
    src_asset: Asset, src_id_dst_map: Dict[int, int], project_src: str, runtime: int, depth: int
) -> Asset:
    """
    Makes a new copy of the asset to be replicated based on the source asset.

    Args:
        src_asset: The asset from the source to be replicated to destination.
        src_id_dst_map: A dictionary of all the mappings of source asset id to destination asset id.
        project_src: The name of the project the object is being replicated from.
        runtime: The timestamp to be used in the new replicated metadata.
        depth: The depth of the asset within the asset hierarchy.

    Returns:
        The replicated asset to be created in the destination.

    """
    logging.debug(f"Creating a new asset based on source event id {src_asset.id}")

    return Asset(
        external_id=src_asset.external_id,
        name=src_asset.name,
        description=src_asset.description,
        metadata=replication.new_metadata(src_asset, project_src, runtime),
        source=src_asset.source,
        parent_id=src_id_dst_map[src_asset.parent_id] if depth > 0 else None,
    )


def build_asset_update(
    src_asset: Asset, dst_asset: Asset, src_id_dst_map: Dict[int, int], project_src: str, runtime: int, depth: int
) -> Asset:
    """
    Makes an updated version of the destination asset based on the corresponding source asset.

    Args:
        src_asset: The asset from the source to be replicated to destination.
        dst_asset: The asset from the destination that needs to be updated to reflect changes made to its source asset.
        src_id_dst_map: **A dictionary of all the mappings of source asset id to destination asset id.
        project_src: The name of the project the object is being replicated from.
        runtime: The timestamp to be used in the new replicated metadata.
        depth: **The depth of the asset within the asset hierarchy.
        ** only needed when hierarchy becomes mutable

    Returns:
        The updated asset object for the replication destination.

    """
    logging.debug(f"Updating existing event {dst_asset.id} based on source event id {src_asset.id}")

    dst_asset.external_id = src_asset.external_id
    dst_asset.name = src_asset.name
    dst_asset.description = src_asset.description
    dst_asset.metadata = replication.new_metadata(src_asset, project_src, runtime)
    dst_asset.source = src_asset.source
    # existing.parent_id = src_id_dst_map[src_asset.parent_id] if depth > 0 else None  # when asset hierarchy is mutable
    return dst_asset


def find_children(assets: List[Asset], parents: List[Asset]) -> List[Asset]:
    """
    Creates a list of all the assets that are children of the parent assets.

    Args:
        assets: A list of all the assets to search for children from.
        parents: A list of all the assets to find children for.

    Returns:
        A list of all the assets that are children to the parents.

    """
    parent_ids = {parent.id for parent in parents} if parents != [None] else parents
    return [asset for asset in assets if asset.parent_id in parent_ids]


def create_hierarchy(
    src_assets: List[Asset], dst_assets: List[Asset], project_src: str, runtime: int, client: CogniteClient
):
    """
    Creates/updates the asset hierarchy in batches by depth, starting with the root assets and then moving on to the
    children of those roots, etc.

    Args:
        src_assets: A list of the assets that are in the source.
        dst_assets: A list of the assets that are in the destination.
        project_src: The name of the project the object is being replicated from.
        runtime: The timestamp to be used in the new replicated metadata.
        client: The client corresponding to the destination project.
    """
    depth = 0
    parents = [None]
    children = find_children(src_assets, parents)

    src_dst_ids: Dict[int, int] = {}
    src_id_dst_asset = replication.make_id_object_map(dst_assets)

    while children:
        logging.info(f"Starting depth {depth}, with {len(children)} assets.")
        create_assets, update_assets, unchanged_assets = replication.make_objects_batch(
            children,
            src_id_dst_asset,
            src_dst_ids,
            build_asset_create,
            build_asset_update,
            project_src,
            runtime,
            depth=depth,
        )

        logging.info(f"Attempting to create {len(create_assets)} assets.")
        created_assets = replication.retry(client.assets.create, create_assets)
        logging.info(f"Attempting to update {len(update_assets)} assets.")
        updated_assets = replication.retry(client.assets.update, update_assets)

        src_dst_ids = replication.existing_mapping(*created_assets, *updated_assets, *unchanged_assets, ids=src_dst_ids)
        logging.debug(f"Dictionary of current asset mappings: {src_dst_ids}")

        num_assets = len(created_assets) + len(updated_assets)
        logging.info(
            f"Finished depth {depth}, updated {len(updated_assets)} and "
            f"posted {len(created_assets)} assets (total of {num_assets} assets)."
        )

        depth += 1
        children = find_children(src_assets, children)

    return src_dst_ids


def remove_not_replicated_in_dst(client_dst: CogniteClient) -> List[Asset]:
    """
    Deleting all the assets in the destination that do not have the "_replicatedSource" in metadata, which
    means that is was not copied from the source, but created in the destination.

    Parameters:
        client_dst: The client corresponding to the destination project.
    """
    asset_ids_to_remove = []
    for asset in client_dst.assets.list(limit=None):
        if not asset.metadata or not asset.metadata["_replicatedSource"]:
            asset_ids_to_remove.append(asset.id)
    client_dst.assets.delete(id=asset_ids_to_remove)
    return asset_ids_to_remove


def remove_replicated_if_not_in_src(src_assets: List[Asset], client_dst: CogniteClient) -> List[Asset]:
    """
    Compare the destination and source assets and delete the ones that are no longer in the source.

    Parameters:
        src_assets: The list of assets from the src destination.
        client_dst: The client corresponding to the destination. project.
    """
    src_asset_ids = {asset.id for asset in src_assets}

    asset_ids_to_remove = []
    for asset in client_dst.assets.list(limit=None):
        if asset.metadata and asset.metadata["_replicatedInternalId"]:
            if int(asset.metadata["_replicatedInternalId"]) in src_asset_ids:
                asset_ids_to_remove.append(asset.id)

    client_dst.assets.delete(id=asset_ids_to_remove)
    return asset_ids_to_remove


def replicate(
    client_src: CogniteClient,
    client_dst: CogniteClient,
    delete_replicated_if_not_in_src: bool = False,
    delete_not_replicated_in_dst: bool = False,
):
    """
    Replicates all the assets from the source project into the destination project.

    Args:
        client_src: The client corresponding to the source project.
        client_dst: The client corresponding to the destination project.
        delete_replicated_if_not_in_src: If True, will delete replicated assets that are in the destination,
        but no longer in the source project (Default=False).
        delete_not_replicated_in_dst: If True, will delete assets from the destination if they were not replicated
        from the source (Default=False).
    """
    project_src = client_src.config.project
    project_dst = client_dst.config.project

    assets_src = client_src.assets.list(limit=None)
    assets_dst = client_dst.assets.list(limit=None)
    logging.info(f"There are {len(assets_src)} existing assets in source ({project_src}).")
    logging.info(f"There are {len(assets_dst)} existing assets in destination ({project_dst}).")

    replicated_runtime = int(time.time()) * 1000
    logging.info(f"These copied/updated assets will have a replicated run time of: {replicated_runtime}.")

    logging.info(
        f"Starting to copy and update {len(assets_src)} assets from "
        f"source ({project_src}) to destination ({project_dst})."
    )
    src_dst_ids_assets = create_hierarchy(assets_src, assets_dst, project_src, replicated_runtime, client_dst)

    logging.info(
        f"Finished copying and updating {len(src_dst_ids_assets)} assets from "
        f"source ({project_src}) to destination ({project_dst})."
    )

    if delete_replicated_if_not_in_src:
        deleted_ids = remove_replicated_if_not_in_src(assets_src, client_dst)
        logging.info(
            f"Deleted {len(deleted_ids)} assets in destination ({project_dst})"
            f" because they were no longer in source ({project_src})   "
        )
    if delete_not_replicated_in_dst:
        deleted_ids = remove_not_replicated_in_dst(client_dst)
        logging.info(
            f"Deleted {len(deleted_ids)} assets in destination ({project_dst}) because"
            f"they were not replicated from source ({project_src})   "
        )